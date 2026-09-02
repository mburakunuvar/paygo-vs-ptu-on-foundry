import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import dotenv_values

from app import (
    BenchmarkDeadlineExceeded,
    Config,
    Executor,
    LoadStats,
    PromptFactory,
    RequestResult,
    Scenario,
    WarmupError,
    Workload,
    _safe_error_message,
    aggregate,
    build_client,
    build_scenarios,
    build_warmup_scenario,
    deployment_order,
    execute,
    execute_with_deadline,
    main,
    nominal_runtime_s,
    readiness_errors,
    run_with_process_watchdog,
    run_closed_loop,
    run_open_loop,
    warm_up,
    write_dependency_snapshot,
    write_manifest,
)


# The published benchmark methodology, as environment variables. Identity values
# are fictional; only the matrix and run-control numbers need to be faithful,
# because the readiness and runtime assertions below pin them.
CANONICAL_ENV = {
    "AZURE_OPENAI_ENDPOINT": "https://benchmark-resource.openai.azure.com/",
    "AZURE_OPENAI_API_VERSION": "v1",
    "AZURE_OPENAI_API_VERSION_VERIFIED": "true",
    "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000002",
    "AZURE_RESOURCE_GROUP": "rg-benchmarks",
    "AZURE_FOUNDRY_RESOURCE": "benchmark-resource",
    "AZURE_FOUNDRY_PROJECT": "benchmarks",
    "AZURE_DEPLOYMENT_GLOBAL_STANDARD": "model-global-standard",
    "AZURE_DEPLOYMENT_PROVISIONED": "model-provisioned",
    "BENCH_SKU_GLOBAL_STANDARD_NAME": "GlobalStandard",
    "BENCH_SKU_GLOBAL_STANDARD_CAPACITY": "1000",
    "BENCH_SKU_PROVISIONED_NAME": "GlobalProvisionedManaged",
    "BENCH_SKU_PROVISIONED_CAPACITY": "35",
    "BENCH_MODEL_NAME": "gpt-5.6-luna",
    "BENCH_MODEL_VERSION": "2026-07-09",
    "BENCH_MODEL_FORMAT": "OpenAI",
    "BENCH_REGION": "swedencentral",
    "BENCH_CLIENT_LOCATION": "test runner",
    "BENCH_CONTENT_FILTER_POLICY": "Microsoft.DefaultV2",
    "BENCH_VERSION_UPGRADE_POLICY": "NoAutoUpgrade",
    "BENCH_ROUTING_SCOPE": "global for both deployment types",
    "BENCH_TARGET_RPM": "357",
    "BENCH_CONCURRENCY_LEVELS": "1,8,32",
    "BENCH_OFFERED_LOAD_RPM": "180,357,530",
    "BENCH_STREAMING_LOAD_RPM": "180,357",
    "BENCH_WORKLOADS": json.dumps(
        [
            {"name": "short-chat", "input_tokens": 200, "max_output_tokens": 100},
            {"name": "rag", "input_tokens": 1000, "max_output_tokens": 300},
            {"name": "long-gen", "input_tokens": 500, "max_output_tokens": 1000},
        ]
    ),
    "BENCH_SEED": "20260727",
    "BENCH_TRIALS": "2",
    "BENCH_TRIAL_DURATION_S": "50",
    "BENCH_INTER_TRIAL_PAUSE_S": "3",
    "BENCH_MAX_RUN_DURATION_S": "8700",
    "BENCH_SHUTDOWN_GRACE_S": "10",
    "BENCH_WARMUP_REQUESTS": "5",
    "BENCH_CONNECT_TIMEOUT_S": "10",
    "BENCH_READ_TIMEOUT_S": "180",
    "BENCH_MAX_IN_FLIGHT": "64",
    "BENCH_TOKEN_PARAM": "max_completion_tokens",
    "BENCH_GENERATION_EXTRA_PARAMS": json.dumps({"reasoning_effort": "none"}),
    "BENCH_OUTPUT_DIR": "results",
}

TERRA_ENV_OVERRIDES = {
    "AZURE_DEPLOYMENT_GLOBAL_STANDARD": "terra-global-standard",
    "AZURE_DEPLOYMENT_PROVISIONED": "terra-provisioned",
    "BENCH_SKU_GLOBAL_STANDARD_CAPACITY": "100",
    "BENCH_MODEL_NAME": "gpt-5.6-terra",
    "BENCH_TARGET_RPM": "35.7",
    "BENCH_OFFERED_LOAD_RPM": "18,35.7,53",
    "BENCH_STREAMING_LOAD_RPM": "18,35.7",
    "BENCH_TRIAL_DURATION_S": "100",
    "BENCH_MAX_RUN_DURATION_S": "10800",
    "BENCH_OUTPUT_DIR": "results/terra",
}


def canonical_env(**overrides):
    """Canonical environment, with overrides applied. None removes a variable."""
    env = dict(CANONICAL_ENV)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def canonical_config(**overrides):
    return Config.from_env(canonical_env(**overrides))


@contextlib.contextmanager
def isolated_env(env):
    """Expose exactly `env` as the AZURE_*/BENCH_* configuration.

    Restoring os.environ wholesale (as patch.dict does) would drop any variable
    whose value is the empty string, because Windows treats setting an empty
    value as deletion in the real process environment. That silently breaks
    subprocesses such as git, so only the configuration namespace is touched and
    every mutated key is restored individually.
    """
    touched = {
        key
        for key in os.environ
        if key.startswith(("AZURE_", "BENCH_"))
    } | set(env)
    saved = {key: os.environ[key] for key in touched if key in os.environ}
    try:
        for key in touched:
            os.environ.pop(key, None)
        os.environ.update(env)
        yield
    finally:
        for key in touched:
            os.environ.pop(key, None)
        os.environ.update(saved)


def request_result(**overrides):
    values = {
        "run_id": "run",
        "scenario_id": "load-rag-r18",
        "pass_name": "offered_load",
        "deployment_label": "global-standard",
        "deployment_name": "deployment",
        "workload": "rag",
        "mode": "nonstream",
        "trial": 0,
        "concurrency": None,
        "offered_rpm": 18.0,
        "worker_id": -1,
        "seq": 1,
        "intended_start_epoch": 1.0,
        "start_epoch": 1.0,
        "end_epoch": 1.1,
        "queue_delay_s": 0.0,
        "total_latency_s": 0.1,
        "ttft_s": None,
        "stream_complete_s": None,
        "mean_output_token_interval_s": None,
        "status": "ok",
        "http_status": 200,
        "error_type": None,
        "error_message": None,
        "throttled": False,
        "prompt_tokens": 1000,
        "completion_tokens": 300,
        "total_tokens": 1300,
        "stream_chunks": None,
        "finish_reason": "stop",
    }
    values.update(overrides)
    return RequestResult(**values)


class BenchmarkMetricTests(unittest.TestCase):
    def test_request_extras_cannot_override_runner_controlled_fields(self):
        cfg = SimpleNamespace(
            token_param="max_completion_tokens",
            extra_generation_params={
                "model": "wrong-deployment",
                "messages": [],
                "max_completion_tokens": 1,
                "temperature": 0.2,
            },
        )
        prompts = SimpleNamespace(build=lambda tokens, variant: "prompt")
        executor = Executor(SimpleNamespace(), cfg, prompts)

        kwargs = executor._kwargs(
            "expected-deployment",
            Workload("rag", 1000, 300),
            False,
            "run:scenario:0:1",
        )

        self.assertEqual(kwargs["model"], "expected-deployment")
        self.assertEqual(kwargs["max_completion_tokens"], 300)
        self.assertEqual(kwargs["messages"][0]["role"], "system")
        self.assertEqual(kwargs["temperature"], 0.2)

    def test_prompt_variants_avoid_cache_reuse_and_remain_repeatable(self):
        prompts = PromptFactory()

        first = prompts.build(1200, "run-a:scenario:0:1")
        repeated = prompts.build(1200, "run-a:scenario:0:1")
        next_request = prompts.build(1200, "run-a:scenario:0:2")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, next_request)
        self.assertNotEqual(first[:256], next_request[:256])

    def test_open_loop_arrival_rate_excludes_request_drain(self):
        rows = [request_result(seq=index) for index in range(18)]

        summary = aggregate(rows, 90.0, arrival_window_s=60.0)

        self.assertEqual(summary["achieved_arrival_rpm"], 18.0)
        self.assertEqual(summary["success_rps"], 0.2)
        self.assertEqual(summary["elapsed_s"], 90.0)
        self.assertEqual(summary["arrival_window_s"], 60.0)

    def test_zero_arrivals_keep_the_aggregate_schema(self):
        summary = aggregate(
            [],
            60.0,
            arrival_window_s=60.0,
            peak_in_flight=0,
            peak_client_backlog=0,
            scheduled_requests=0,
        )

        self.assertEqual(summary["requests"], 0)
        self.assertEqual(summary["scheduled_requests"], 0)
        self.assertEqual(summary["achieved_arrival_rpm"], 0.0)
        self.assertEqual(summary["arrival_window_s"], 60.0)
        self.assertEqual(summary["peak_client_backlog"], 0)
        self.assertIsNone(summary["rates"]["throttled_429"])

    def test_warmup_scenario_is_always_nonstreaming(self):
        scenario = build_warmup_scenario(Workload("rag", 1000, 300))

        self.assertEqual(scenario.pass_name, "warmup")
        self.assertEqual(scenario.mode, "nonstream")
        self.assertEqual(scenario.concurrency, 1)

    def test_cadence_quantiles_report_their_usable_sample_count(self):
        rows = [request_result(seq=index) for index in range(100)]
        rows[0].mean_output_token_interval_s = 0.2

        summary = aggregate(rows, 60.0)
        cadence = summary["mean_output_token_interval_s"]

        self.assertEqual(cadence["samples"], 1)
        self.assertEqual(cadence["p50"], 0.2)
        self.assertIn("fewer than 100", cadence["sample_warning"])

    def test_stream_completion_and_other_errors_are_aggregated(self):
        rows = [
            request_result(mode="stream", stream_complete_s=0.5),
            request_result(seq=2, mode="stream", stream_complete_s=1.5),
            request_result(
                seq=3,
                status="invalid_response",
                http_status=200,
                error_type="InvalidResponseError",
                error_message="missing usage",
            ),
        ]

        summary = aggregate(rows, 2.0)

        self.assertEqual(summary["stream_completion_s"]["p50"], 1.0)
        self.assertEqual(summary["stream_completion_s"]["samples"], 2)
        self.assertEqual(summary["rates"]["invalid_response"], 0.3333)
        self.assertEqual(summary["rates"]["other_error"], 0.3333)


class BenchmarkSecurityTests(unittest.TestCase):
    def test_client_revalidates_endpoint_before_creating_credentials(self):
        cfg = canonical_config(AZURE_OPENAI_ENDPOINT="https://example.com")

        with self.assertRaisesRegex(
            ValueError,
            "endpoint host is not a supported public Azure OpenAI domain",
        ):
            build_client(cfg)

    def test_readiness_rejects_endpoints_that_could_expose_a_bearer_token(self):
        cases = (
            (
                "http://benchmark-resource.openai.azure.com",
                "endpoint must use HTTPS",
            ),
            (
                "https://example.com",
                "endpoint host is not a supported public Azure OpenAI domain",
            ),
            (
                "https://" + "user:placeholder@"
                "benchmark-resource.openai.azure.com",
                "endpoint must not include credentials",
            ),
            (
                "https://benchmark-resource.openai.azure.com/openai/v1",
                "endpoint must be a resource root without a path, query, or fragment",
            ),
            (
                "https://benchmark-resource.openai.azure.com:8443",
                "endpoint must use the default HTTPS port",
            ),
        )

        for endpoint, expected_error in cases:
            with self.subTest(endpoint=endpoint):
                errors = readiness_errors(
                    canonical_config(AZURE_OPENAI_ENDPOINT=endpoint),
                    ["global-standard"],
                )
                self.assertIn(expected_error, errors)

    def test_readiness_accepts_public_azure_endpoint(self):
        cfg = canonical_config(
            AZURE_OPENAI_ENDPOINT=(
                "https://benchmark-resource.openai.azure.com/"
            )
        )

        self.assertEqual(readiness_errors(cfg, ["global-standard"]), [])

    def test_recorded_errors_redact_credentials_and_control_characters(self):
        authorization = (
            "Authorization" + ": Bearer " + "header.payload.signature"
        )
        api_key = "api_" + "key=placeholder-one"
        password = "pass" + "word='placeholder-two'"
        json_secret = '"client_' + 'secret":"placeholder-three"'
        private_key = (
            "-----BEGIN " + "PRIVATE KEY-----\nplaceholder\n"
            "-----END " + "PRIVATE KEY-----"
        )
        message = _safe_error_message(
            RuntimeError(
                f"{authorization}\n{api_key}; {password}; {json_secret}"
                f"\x1b[31m {private_key}"
            )
        )

        self.assertNotIn("header.payload.signature", message)
        self.assertNotIn("placeholder-one", message)
        self.assertNotIn("placeholder-two", message)
        self.assertNotIn("placeholder-three", message)
        self.assertNotIn("PRIVATE KEY-----", message)
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)
        self.assertIn("<redacted>", message)

    def test_readiness_rejects_credentials_in_generation_parameters(self):
        cfg = canonical_config(
            BENCH_GENERATION_EXTRA_PARAMS=json.dumps(
                {
                    "temperature": 0.2,
                    "metadata": {"client_secret": "placeholder"},
                }
            )
        )

        errors = readiness_errors(cfg, ["global-standard"])

        self.assertIn(
            "generation.extra_params must not contain credential fields: "
            "generation.extra_params.metadata.client_secret",
            errors,
        )


class BenchmarkAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_delay_uses_monotonic_time(self):
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=3,
            total_tokens=13,
        )
        response = SimpleNamespace(
            usage=usage,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="response"),
                    finish_reason="stop",
                )
            ],
        )

        async def create(**kwargs):
            return response

        cfg = SimpleNamespace(
            token_param="max_completion_tokens",
            extra_generation_params={},
        )
        executor = Executor(
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=create)
                )
            ),
            cfg,
            SimpleNamespace(build=lambda tokens, variant: "prompt"),
        )
        scenario = Scenario(
            scenario_id="load-rag-r18",
            pass_name="offered_load",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=None,
            offered_rpm=18.0,
        )

        with (
            patch("app.time.time", side_effect=[1000.0, 1001.0]),
            patch("app.time.perf_counter", side_effect=[10.0, 11.0]),
        ):
            result = await executor.run(
                scenario=scenario,
                deployment_label="global-standard",
                deployment_name="deployment",
                trial=0,
                worker_id=0,
                seq=1,
                run_id="run",
                intended_start_epoch=100.0,
                intended_start_perf=9.5,
            )

        self.assertEqual(result.queue_delay_s, 0.5)

    async def test_empty_success_response_is_classified_as_invalid(self):
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=3,
            total_tokens=13,
        )
        response = SimpleNamespace(
            usage=usage,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=""),
                    finish_reason="stop",
                )
            ],
        )

        async def create(**kwargs):
            return response

        cfg = SimpleNamespace(
            token_param="max_completion_tokens",
            extra_generation_params={},
        )
        executor = Executor(
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=create)
                )
            ),
            cfg,
            SimpleNamespace(build=lambda tokens, variant: "prompt"),
        )
        scenario = Scenario(
            scenario_id="conc-rag-c1",
            pass_name="concurrency",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=1,
            offered_rpm=None,
        )

        result = await executor.run(
            scenario=scenario,
            deployment_label="global-standard",
            deployment_name="deployment",
            trial=0,
            worker_id=0,
            seq=1,
            run_id="run",
            intended_start_epoch=None,
        )

        self.assertEqual(result.status, "invalid_response")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.error_type, "InvalidResponseError")

    async def test_all_warmups_complete_before_first_measurement(self):
        events = []
        workloads = (Workload("short", 100, 50), Workload("rag", 1000, 300))
        scenario = Scenario(
            scenario_id="conc-short-c1",
            pass_name="concurrency",
            workload=workloads[0],
            mode="nonstream",
            concurrency=1,
            offered_rpm=None,
        )

        class Resource:
            async def close(self):
                pass

            async def aclose(self):
                pass

        class FakeExecutor:
            def __init__(self, client, cfg, prompts):
                pass

        async def record_warmup(executor, cfg, warmup, label, name, run_id):
            events.append(("warmup", label, warmup.workload.name))

        async def record_measurement(*args, **kwargs):
            events.append(("measurement", kwargs["deployment_label"]))
            return kwargs["results"]

        cfg = SimpleNamespace(
            deployments={"global-standard": "gs", "provisioned": "ptu"},
            workloads=workloads,
            seed=1,
            trials=1,
            trial_duration_s=0.001,
            inter_trial_pause_s=0,
        )
        resource = Resource()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("app.build_scenarios", return_value=[scenario]),
                patch("app.PromptFactory", return_value=object()),
                patch(
                    "app.build_client",
                    return_value=(resource, resource, resource),
                ),
                patch("app.Executor", FakeExecutor),
                patch("app.warm_up", new=record_warmup),
                patch("app.run_closed_loop", new=record_measurement),
            ):
                await execute(
                    cfg,
                    ["global-standard", "provisioned"],
                    "run",
                    Path(directory),
                )

        self.assertEqual(
            events[:4],
            [
                ("warmup", "global-standard", "short"),
                ("warmup", "global-standard", "rag"),
                ("warmup", "provisioned", "short"),
                ("warmup", "provisioned", "rag"),
            ],
        )
        self.assertEqual(events[4][0], "measurement")

    async def test_closed_loop_start_rate_excludes_request_drain(self):
        class SlowExecutor:
            async def run(self, **kwargs):
                await asyncio.sleep(0.02)
                return request_result(
                    seq=kwargs["seq"],
                    pass_name="concurrency",
                    scenario_id="conc-rag-c1",
                    concurrency=1,
                    offered_rpm=None,
                )

        scenario = Scenario(
            scenario_id="conc-rag-c1",
            pass_name="concurrency",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=1,
            offered_rpm=None,
        )
        stats = LoadStats()

        rows = await run_closed_loop(
            SlowExecutor(),
            scenario,
            deployment_label="global-standard",
            deployment_name="deployment",
            trial=0,
            run_id="run",
            duration_s=0.01,
            stats=stats,
        )
        summary = aggregate(
            rows,
            0.02,
            arrival_window_s=stats.arrival_window_s,
            scheduled_requests=stats.scheduled_requests,
        )

        self.assertEqual(stats.arrival_window_s, 0.01)
        self.assertEqual(stats.scheduled_requests, 1)
        self.assertEqual(summary["achieved_arrival_rpm"], 6000.0)
        self.assertEqual(summary["success_rps"], 50.0)

    async def test_stream_cadence_uses_completion_tokens_not_chunks(self):
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=3,
            total_tokens=13,
        )
        events = [
            SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="first"),
                        finish_reason=None,
                    )
                ],
            ),
            SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="second"),
                        finish_reason="stop",
                    )
                ],
            ),
            SimpleNamespace(usage=usage, choices=[]),
        ]

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not events:
                    raise StopAsyncIteration
                return events.pop(0)

        completions = SimpleNamespace(create=lambda **kwargs: None)

        async def create(**kwargs):
            return Stream()

        completions.create = create
        executor = object.__new__(Executor)
        executor._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch("app.time.perf_counter", side_effect=[10.0, 12.0, 14.0]):
            result = await executor._stream_once({}, 8.0)

        self.assertEqual(result[0], 2.0)
        self.assertEqual(result[1], 6.0)
        self.assertEqual(result[2], 1.0)
        self.assertEqual(result[3], 2)
        self.assertEqual(result[5], 3)

    async def test_stream_cadence_is_unavailable_for_one_coalesced_chunk(self):
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=3,
            total_tokens=13,
        )
        events = [
            SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="three tokens together"),
                        finish_reason="stop",
                    )
                ],
            ),
            SimpleNamespace(usage=usage, choices=[]),
        ]

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not events:
                    raise StopAsyncIteration
                return events.pop(0)

        async def create(**kwargs):
            return Stream()

        executor = object.__new__(Executor)
        executor._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )

        with patch("app.time.perf_counter", side_effect=[10.0, 14.0]):
            result = await executor._stream_once({}, 8.0)

        self.assertIsNone(result[2])

    async def test_open_loop_tracks_waiting_client_backlog(self):
        class FixedRng:
            def expovariate(self, rate):
                return 0.001

        class SlowExecutor:
            async def run(self, **kwargs):
                await asyncio.sleep(0.02)
                return request_result(seq=kwargs["seq"])

        workload = Workload("rag", 1000, 300)
        scenario = Scenario(
            scenario_id="load-rag-r18",
            pass_name="offered_load",
            workload=workload,
            mode="nonstream",
            concurrency=None,
            offered_rpm=18.0,
        )

        rows, peak_in_flight, peak_backlog = await run_open_loop(
            SlowExecutor(),
            scenario,
            deployment_label="global-standard",
            deployment_name="deployment",
            trial=0,
            run_id="run",
            duration_s=0.006,
            rng=FixedRng(),
            max_in_flight=1,
        )

        self.assertGreater(len(rows), 1)
        self.assertEqual(peak_in_flight, 1)
        self.assertGreater(peak_backlog, 0)

    async def test_open_loop_uses_bounded_worker_tasks(self):
        class FixedRng:
            def expovariate(self, rate):
                return 0.001

        class SlowExecutor:
            def __init__(self):
                self.request_tasks = set()

            async def run(self, **kwargs):
                self.request_tasks.add(asyncio.current_task())
                await asyncio.sleep(0.01)
                return request_result(seq=kwargs["seq"])

        scenario = Scenario(
            scenario_id="load-rag-r60000",
            pass_name="offered_load",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=None,
            offered_rpm=60000.0,
        )
        executor = SlowExecutor()

        rows, peak_in_flight, _ = await run_open_loop(
            executor,
            scenario,
            deployment_label="global-standard",
            deployment_name="deployment",
            trial=0,
            run_id="run",
            duration_s=0.006,
            rng=FixedRng(),
            max_in_flight=2,
        )

        self.assertGreater(len(rows), 2)
        self.assertEqual(peak_in_flight, 2)
        self.assertEqual(len(executor.request_tasks), 2)

    async def test_zero_arrival_open_loop_honors_full_window(self):
        class NoArrivalRng:
            def expovariate(self, rate):
                return 1.0

        class UnusedExecutor:
            async def run(self, **kwargs):
                raise AssertionError("no request should be issued")

        scenario = Scenario(
            scenario_id="load-rag-r4",
            pass_name="offered_load",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=None,
            offered_rpm=4.0,
        )
        stats = LoadStats()
        started = asyncio.get_running_loop().time()

        rows, _, _ = await run_open_loop(
            UnusedExecutor(),
            scenario,
            deployment_label="global-standard",
            deployment_name="deployment",
            trial=0,
            run_id="run",
            duration_s=0.01,
            rng=NoArrivalRng(),
            max_in_flight=1,
            stats=stats,
        )

        self.assertEqual(rows, [])
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.009)
        self.assertEqual(stats.arrival_window_s, 0.01)
        self.assertEqual(stats.scheduled_requests, 0)

    async def test_canceling_open_loop_cancels_and_joins_children(self):
        child_started = asyncio.Event()

        class FixedRng:
            def expovariate(self, rate):
                return 0.001

        class BlockingExecutor:
            def __init__(self):
                self.active = 0
                self.cancelled = 0

            async def run(self, **kwargs):
                self.active += 1
                child_started.set()
                try:
                    await asyncio.sleep(10)
                    return request_result(seq=kwargs["seq"])
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
                finally:
                    self.active -= 1

        executor = BlockingExecutor()
        scenario = Scenario(
            scenario_id="load-rag-r60000",
            pass_name="offered_load",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=None,
            offered_rpm=60000.0,
        )
        task = asyncio.create_task(
            run_open_loop(
                executor,
                scenario,
                deployment_label="global-standard",
                deployment_name="deployment",
                trial=0,
                run_id="run",
                duration_s=10,
                rng=FixedRng(),
                max_in_flight=2,
            )
        )
        await child_started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(executor.active, 0)
        self.assertGreater(executor.cancelled, 0)

    async def test_deadline_preserves_active_trial_rows_and_partial_aggregate(self):
        class Resource:
            async def close(self):
                pass

            async def aclose(self):
                pass

        class FastExecutor:
            def __init__(self, client, cfg, prompts):
                self.calls = 0

            async def run(self, **kwargs):
                self.calls += 1
                if self.calls > 1:
                    await asyncio.sleep(10)
                scenario = kwargs["scenario"]
                return request_result(
                    run_id=kwargs["run_id"],
                    scenario_id=scenario.scenario_id,
                    pass_name=scenario.pass_name,
                    deployment_label=kwargs["deployment_label"],
                    deployment_name=kwargs["deployment_name"],
                    workload=scenario.workload.name,
                    mode=scenario.mode,
                    trial=kwargs["trial"],
                    seq=kwargs["seq"],
                    offered_rpm=scenario.offered_rpm,
                )

        async def no_warmup(*args, **kwargs):
            pass

        scenario = Scenario(
            scenario_id="load-rag-r60000000",
            pass_name="offered_load",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=None,
            offered_rpm=60000000.0,
        )
        cfg = SimpleNamespace(
            deployments={"global-standard": "deployment"},
            workloads=(scenario.workload,),
            seed=1,
            trials=1,
            warmup_requests=1,
            trial_duration_s=1.0,
            max_in_flight=2,
            inter_trial_pause_s=0.0,
            max_run_duration_s=0.05,
        )

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            resource = Resource()
            with (
                patch("app.build_scenarios", return_value=[scenario]),
                patch("app.PromptFactory", return_value=object()),
                patch(
                    "app.build_client",
                    return_value=(resource, resource, resource),
                ),
                patch("app.Executor", FastExecutor),
                patch("app.warm_up", new=no_warmup),
            ):
                with self.assertRaises(BenchmarkDeadlineExceeded):
                    await execute_with_deadline(
                        cfg,
                        ["global-standard"],
                        "run",
                        out_dir,
                    )

            rows = (out_dir / "requests.jsonl").read_text().splitlines()
            aggregates = json.loads((out_dir / "aggregates.json").read_text())

        self.assertGreater(len(rows), 0)
        self.assertEqual(len(aggregates), 1)
        self.assertTrue(aggregates[0]["partial"])
        self.assertEqual(aggregates[0]["requests"], len(rows))
        self.assertGreater(aggregates[0]["scheduled_requests"], 0)

    async def test_completed_aggregate_is_checkpointed_during_next_trial(self):
        second_trial_started = asyncio.Event()
        measurement_calls = 0

        class Resource:
            async def close(self):
                pass

            async def aclose(self):
                pass

        class FakeExecutor:
            def __init__(self, client, cfg, prompts):
                pass

        async def no_warmup(*args, **kwargs):
            pass

        async def checkpoint_then_block(*args, **kwargs):
            nonlocal measurement_calls
            measurement_calls += 1
            if measurement_calls == 1:
                scenario_arg = args[1]
                row = request_result(
                    scenario_id=scenario_arg.scenario_id,
                    pass_name="concurrency",
                    concurrency=1,
                    offered_rpm=None,
                )
                kwargs["results"].append(row)
                kwargs["on_result"](row)
                return kwargs["results"]
            second_trial_started.set()
            await asyncio.Future()

        scenario = Scenario(
            scenario_id="conc-rag-c1",
            pass_name="concurrency",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=1,
            offered_rpm=None,
        )
        cfg = SimpleNamespace(
            deployments={"global-standard": "deployment"},
            workloads=(scenario.workload,),
            seed=1,
            trials=2,
            trial_duration_s=0.001,
            inter_trial_pause_s=0.0,
        )

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            resource = Resource()
            with (
                patch("app.build_scenarios", return_value=[scenario]),
                patch("app.PromptFactory", return_value=object()),
                patch(
                    "app.build_client",
                    return_value=(resource, resource, resource),
                ),
                patch("app.Executor", FakeExecutor),
                patch("app.warm_up", new=no_warmup),
                patch("app.run_closed_loop", new=checkpoint_then_block),
            ):
                task = asyncio.create_task(
                    execute(cfg, ["global-standard"], "run", out_dir)
                )
                try:
                    await asyncio.wait_for(second_trial_started.wait(), 1)
                    checkpoint = json.loads(
                        (out_dir / "aggregates.json").read_text()
                    )
                    self.assertEqual(len(checkpoint), 1)
                    self.assertFalse(checkpoint[0]["partial"])
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

    async def test_failed_warmup_aborts(self):
        class FailedExecutor:
            async def run(self, **kwargs):
                return request_result(
                    status="http_error",
                    http_status=401,
                    error_message="unauthorized",
                )

        cfg = SimpleNamespace(warmup_requests=1)
        scenario = Scenario(
            scenario_id="conc-rag-c1",
            pass_name="concurrency",
            workload=Workload("rag", 1000, 300),
            mode="nonstream",
            concurrency=1,
            offered_rpm=None,
        )

        with self.assertRaisesRegex(WarmupError, "401.*unauthorized"):
            await warm_up(
                FailedExecutor(),
                cfg,
                scenario,
                "global-standard",
                "deployment",
                "run",
            )

    async def test_wall_clock_deadline_cancels_execution(self):
        async def slow_execute(cfg, labels, run_id, out_dir):
            await asyncio.sleep(1)

        cfg = SimpleNamespace(max_run_duration_s=0.001)
        with patch("app.execute", new=slow_execute):
            with self.assertRaises(BenchmarkDeadlineExceeded):
                await execute_with_deadline(cfg, [], "run", Path("unused"))


class BenchmarkManifestTests(unittest.TestCase):
    def test_dependency_snapshot_records_versions_without_source_urls(self):
        completed = SimpleNamespace(stdout="example-package==1.2.3\n")

        with tempfile.TemporaryDirectory() as directory:
            with patch("app.subprocess.run", return_value=completed) as run:
                snapshot = write_dependency_snapshot(Path(directory))

            content = (Path(directory) / snapshot["file"]).read_text(
                encoding="utf-8"
            )

        self.assertEqual(snapshot["file"], "pip-packages.txt")
        self.assertEqual(content, "example-package==1.2.3\n")
        run.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "pip",
                "list",
                "--format=freeze",
                "--disable-pip-version-check",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_config_rejects_lossy_numeric_coercion(self):
        invalid_values = (
            ("BENCH_TRIALS", "true"),
            ("BENCH_WARMUP_REQUESTS", "1.9"),
            ("BENCH_MAX_IN_FLIGHT", "true"),
            ("BENCH_TRIAL_DURATION_S", "fast"),
            ("BENCH_CONCURRENCY_LEVELS", "1,8,thirty-two"),
            ("AZURE_OPENAI_API_VERSION_VERIFIED", "maybe"),
            ("BENCH_WORKLOADS", "not-json"),
        )
        for name, value in invalid_values:
            with self.subTest(field=name):
                with self.assertRaisesRegex(ValueError, name):
                    Config.from_env(canonical_env(**{name: value}))

    def test_config_reports_missing_workload_fields_clearly(self):
        workloads = json.dumps(
            [{"name": "rag", "input_tokens": 1000}]
        )

        with self.assertRaisesRegex(
            ValueError,
            "workload is missing required field.*max_output_tokens",
        ):
            Config.from_env(canonical_env(BENCH_WORKLOADS=workloads))

    def test_manifest_records_reproducibility_contract(self):
        cfg = canonical_config()
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_manifest(
                cfg,
                ["global-standard"],
                "run",
                out_dir,
                ".env",
                {"file": "pip-packages.txt", "sha256": "digest"},
            )
            manifest = json.loads((out_dir / "manifest.json").read_text())

        self.assertIn("source", manifest)
        self.assertEqual(len(manifest["source"]["commit"]), 40)
        self.assertEqual(len(manifest["runner_sha256"]), 64)
        self.assertEqual(len(manifest["config_sha256"]), 64)
        self.assertEqual(manifest["config_source"], ".env")
        self.assertIn("experiment", manifest)
        self.assertEqual(manifest["retry_policy"]["attempts_per_request"], 1)
        self.assertEqual(manifest["config"]["max_run_duration_s"], 8700)
        self.assertEqual(manifest["config"]["max_in_flight"], 64)
        self.assertEqual(
            manifest["config"]["prompt_cache_strategy"],
            "unique request marker before repeated prompt content",
        )
        self.assertEqual(manifest["dependency_snapshot"]["sha256"], "digest")
        self.assertEqual(manifest["client"]["executable"], Path(sys.executable).name)

    def test_manifest_defensively_redacts_sensitive_generation_fields(self):
        cfg = replace(
            canonical_config(),
            extra_generation_params={
                "metadata": {"access_token": "placeholder"}
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_manifest(
                cfg,
                ["global-standard"],
                "run",
                out_dir,
                ".env",
                {"file": "pip-packages.txt", "sha256": "digest"},
            )
            raw_manifest = (out_dir / "manifest.json").read_text(
                encoding="utf-8"
            )
            manifest = json.loads(raw_manifest)

        self.assertNotIn("placeholder", raw_manifest)
        self.assertEqual(
            manifest["config"]["extra_generation_params"]["metadata"][
                "access_token"
            ],
            "<redacted>",
        )

    def test_config_digest_tracks_the_effective_values(self):
        def digest(cfg):
            with tempfile.TemporaryDirectory() as directory:
                out_dir = Path(directory)
                write_manifest(
                    cfg,
                    ["global-standard"],
                    "run",
                    out_dir,
                    ".env",
                    {"file": "pip-packages.txt", "sha256": "digest"},
                )
                return json.loads(
                    (out_dir / "manifest.json").read_text()
                )["config_sha256"]

        baseline = digest(canonical_config())

        self.assertEqual(baseline, digest(canonical_config()))
        self.assertNotEqual(baseline, digest(canonical_config(BENCH_SEED="1")))
        self.assertNotEqual(
            baseline, digest(canonical_config(BENCH_CONCURRENCY_LEVELS="1,8"))
        )
        self.assertNotEqual(
            baseline, digest(canonical_config(BENCH_MODEL_VERSION="2026-01-01"))
        )

    def test_current_full_matrix_fits_with_operational_slack(self):
        cfg = canonical_config()

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertEqual(errors, [])
        self.assertEqual(len(build_scenarios(cfg)), 24)
        self.assertEqual(cfg.trials, 2)
        self.assertEqual(nominal_runtime_s(cfg, list(cfg.deployments)), 5088)

    def test_two_trials_balance_deployment_order(self):
        labels = ["global-standard", "provisioned"]

        self.assertEqual(deployment_order(labels, 0), labels)
        self.assertEqual(deployment_order(labels, 1), list(reversed(labels)))

    def test_terra_profile_parses_and_fits_its_runtime_budget(self):
        cfg = canonical_config(**TERRA_ENV_OVERRIDES)

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertEqual(errors, [])
        self.assertEqual(cfg.experiment["model_name"], "gpt-5.6-terra")
        self.assertEqual(cfg.experiment["target_rpm"], 35.7)
        self.assertEqual(
            cfg.experiment["deployment_skus"]["global-standard"]["capacity"],
            100,
        )
        self.assertEqual(cfg.offered_load_rpm, (18.0, 35.7, 53.0))
        self.assertEqual(cfg.trials, 2)
        self.assertEqual(cfg.max_run_duration_s, 10800)
        self.assertEqual(nominal_runtime_s(cfg, list(cfg.deployments)), 9888)

    def test_tracked_profile_templates_are_runnable_when_completed(self):
        resource_fields = (
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_VERSION_VERIFIED",
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_TENANT_ID",
            "AZURE_RESOURCE_GROUP",
            "AZURE_FOUNDRY_RESOURCE",
            "AZURE_FOUNDRY_PROJECT",
            "AZURE_DEPLOYMENT_GLOBAL_STANDARD",
            "AZURE_DEPLOYMENT_PROVISIONED",
            "BENCH_REGION",
            "BENCH_CLIENT_LOCATION",
        )
        resource_values = {
            name: CANONICAL_ENV[name]
            for name in resource_fields
        }
        profiles = (
            (".env.luna.example", "gpt-5.6-luna", 357, Path("results/luna")),
            (".env.terra.example", "gpt-5.6-terra", 35.7, Path("results/terra")),
        )

        for filename, model, target_rpm, output_dir in profiles:
            with self.subTest(profile=filename):
                parsed = dotenv_values(
                    Path(__file__).with_name(filename),
                    interpolate=False,
                )
                profile_env = {
                    name: value
                    for name, value in parsed.items()
                    if value is not None
                }
                profile_env.update(resource_values)
                cfg = Config.from_env(profile_env)

                self.assertEqual(
                    readiness_errors(
                        cfg,
                        list(cfg.deployments),
                        require_reference_pair=True,
                    ),
                    [],
                )
                self.assertEqual(cfg.experiment["model_name"], model)
                self.assertEqual(cfg.experiment["target_rpm"], target_rpm)
                self.assertEqual(cfg.output_dir, output_dir)

    def test_manifest_digest_distinguishes_luna_and_terra_profiles(self):
        def digest(cfg):
            with tempfile.TemporaryDirectory() as directory:
                out_dir = Path(directory)
                write_manifest(
                    cfg,
                    list(cfg.deployments),
                    "run",
                    out_dir,
                    ".env",
                    {"file": "pip-packages.txt", "sha256": "digest"},
                )
                return json.loads(
                    (out_dir / "manifest.json").read_text(encoding="utf-8")
                )["config_sha256"]

        self.assertNotEqual(
            digest(canonical_config()),
            digest(canonical_config(**TERRA_ENV_OVERRIDES)),
        )

    def test_readiness_requires_baseline_and_target_load_coverage(self):
        cfg = canonical_config()
        cfg = replace(
            cfg,
            concurrency_levels=(8, 32),
            offered_load_rpm=(18.0, 27.0),
            streaming_load_rpm=(18.0,),
            experiment={**cfg.experiment, "target_rpm": 18},
        )

        errors = readiness_errors(cfg, ["global-standard"])

        self.assertIn("concurrency_levels must include baseline level 1", errors)
        self.assertIn(
            "offered_load_rpm must include a level below experiment.target_rpm",
            errors,
        )
        self.assertIn(
            "streaming_load_rpm must include a level below experiment.target_rpm",
            errors,
        )

    def test_full_comparison_requires_both_reference_deployments(self):
        cfg = Config.from_env(
            canonical_env(
                AZURE_DEPLOYMENT_PROVISIONED=None,
                BENCH_SKU_PROVISIONED_NAME=None,
                BENCH_SKU_PROVISIONED_CAPACITY=None,
            )
        )

        full_run_errors = readiness_errors(
            cfg,
            list(cfg.deployments),
            require_reference_pair=True,
        )

        self.assertIn(
            "full comparison is missing deployment label(s): provisioned; "
            "set AZURE_DEPLOYMENT_PROVISIONED",
            full_run_errors,
        )
        self.assertEqual(
            readiness_errors(cfg, ["global-standard"]),
            [],
        )

    def test_dry_run_reports_readiness_errors_without_network_calls(self):
        output = io.StringIO()
        errors = io.StringIO()
        env = canonical_env(AZURE_DEPLOYMENT_PROVISIONED="<unresolved>")

        with (
            isolated_env(env),
            patch.object(
                sys,
                "argv",
                ["app.py", "--no-env-file", "--dry-run"],
            ),
            patch(
                "app.build_client",
                side_effect=AssertionError("dry run contacted the network"),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertIn("dry-run readiness failed", errors.getvalue())
        self.assertIn("deployment name is unresolved for provisioned", errors.getvalue())
        self.assertIn("no network calls made", output.getvalue())

    def test_dry_run_names_the_environment_variables_that_are_unset(self):
        output = io.StringIO()
        errors = io.StringIO()
        env = canonical_env(
            AZURE_OPENAI_ENDPOINT=None,
            AZURE_SUBSCRIPTION_ID=None,
            BENCH_SKU_PROVISIONED_CAPACITY=None,
        )

        with (
            isolated_env(env),
            patch.object(
                sys,
                "argv",
                ["app.py", "--no-env-file", "--dry-run"],
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            exit_code = main()

        reported = errors.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("endpoint is unresolved", reported)
        self.assertIn("experiment.subscription_id is unresolved", reported)
        self.assertIn("unset environment variables:", reported)
        self.assertIn("AZURE_OPENAI_ENDPOINT", reported)
        self.assertIn("AZURE_SUBSCRIPTION_ID", reported)
        self.assertIn("BENCH_SKU_PROVISIONED_CAPACITY", reported)
        self.assertNotIn("AZURE_TENANT_ID\n", reported)
        self.assertIn("no network calls made", output.getvalue())

    def test_environment_overrides_the_dotenv_file(self):
        env_file_body = "\n".join(
            f"{name}={value}" for name, value in canonical_env().items()
        )
        captured_configs = []
        errors = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(env_file_body, encoding="utf-8")
            with (
                isolated_env({"BENCH_MAX_IN_FLIGHT": "7"}),
                patch.object(
                    sys,
                    "argv",
                    ["app.py", "--env-file", str(env_path), "--dry-run"],
                ),
                patch(
                    "app.print_matrix",
                    side_effect=lambda cfg, labels: captured_configs.append(cfg),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(errors),
            ):
                exit_code = main()

        cfg = captured_configs[0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(cfg.max_in_flight, 7)
        self.assertEqual(cfg.seed, 20260727)
        self.assertEqual(cfg.deployments["provisioned"], "model-provisioned")
        self.assertIn(
            "process environment overrides .env: BENCH_MAX_IN_FLIGHT",
            errors.getvalue(),
        )

    def test_dotenv_values_are_not_exported_to_the_process(self):
        env_file_body = "\n".join(
            f"{name}={value}" for name, value in canonical_env().items()
        )

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env.luna"
            env_path.write_text(env_file_body, encoding="utf-8")
            with (
                isolated_env({}),
                patch.object(
                    sys,
                    "argv",
                    ["app.py", "--env-file", str(env_path), "--dry-run"],
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = main()
                self.assertNotIn("AZURE_OPENAI_ENDPOINT", os.environ)
                self.assertNotIn("BENCH_MODEL_NAME", os.environ)

        self.assertEqual(exit_code, 0)

    def test_env_file_ignores_undeclared_ambient_deployments(self):
        env_file_body = "\n".join(
            f"{name}={value}" for name, value in canonical_env().items()
        )
        captured_configs = []

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env.luna"
            env_path.write_text(env_file_body, encoding="utf-8")
            with (
                isolated_env(
                    {
                        "AZURE_DEPLOYMENT_DATA_ZONE": "unrelated-deployment",
                        "BENCH_SKU_DATA_ZONE_NAME": "DataZoneStandard",
                        "BENCH_SKU_DATA_ZONE_CAPACITY": "20",
                    }
                ),
                patch.object(
                    sys,
                    "argv",
                    ["app.py", "--env-file", str(env_path), "--dry-run"],
                ),
                patch(
                    "app.print_matrix",
                    side_effect=lambda cfg, labels: captured_configs.append(cfg),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            sorted(captured_configs[0].deployments),
            ["global-standard", "provisioned"],
        )

    def test_empty_explicit_env_file_does_not_fall_back_to_ambient_config(self):
        errors = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env.empty"
            env_path.write_text("", encoding="utf-8")
            with (
                isolated_env(canonical_env()),
                patch.object(
                    sys,
                    "argv",
                    ["app.py", "--env-file", str(env_path), "--dry-run"],
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(errors),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertIn("endpoint is unresolved", errors.getvalue())

    def test_explicit_missing_dotenv_file_is_rejected(self):
        errors = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "missing.env"
            with (
                isolated_env(canonical_env()),
                patch.object(
                    sys,
                    "argv",
                    ["app.py", "--env-file", str(missing_path), "--dry-run"],
                ),
                contextlib.redirect_stderr(errors),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertIn("dotenv file not found", errors.getvalue())

    def test_deployment_labels_come_from_the_environment(self):
        cfg = Config.from_env(
            canonical_env(
                AZURE_DEPLOYMENT_DATA_ZONE="model-data-zone",
                BENCH_SKU_DATA_ZONE_NAME="DataZoneStandard",
                BENCH_SKU_DATA_ZONE_CAPACITY="20",
            )
        )

        self.assertEqual(
            sorted(cfg.deployments),
            ["data-zone", "global-standard", "provisioned"],
        )
        self.assertEqual(cfg.deployments["data-zone"], "model-data-zone")
        self.assertEqual(
            cfg.experiment["deployment_skus"]["data-zone"],
            {"name": "DataZoneStandard", "capacity": 20},
        )
        self.assertEqual(readiness_errors(cfg, ["data-zone"]), [])

    def test_readiness_rejects_zero_warmup_and_incomplete_sku_metadata(self):
        cfg = canonical_config()
        cfg = replace(
            cfg,
            warmup_requests=0,
            experiment={**cfg.experiment, "deployment_skus": {}},
        )

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertIn("warmup_requests must be at least 1", errors)
        self.assertTrue(
            any("deployment_skus.global-standard is missing" in error for error in errors)
        )
        self.assertTrue(
            any("deployment_skus.provisioned is missing" in error for error in errors)
        )

    def test_readiness_requires_two_trials_and_rejects_reserved_extras(self):
        cfg = canonical_config()
        cfg = replace(
            cfg,
            trials=1,
            extra_generation_params={"model": "wrong-deployment"},
        )

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertIn("trials must be at least 2", errors)
        self.assertTrue(
            any("runner-controlled keys: model" in error for error in errors)
        )

    def test_readiness_rejects_prompt_cache_controls(self):
        cfg = canonical_config(
            BENCH_GENERATION_EXTRA_PARAMS=json.dumps(
                {"prompt_cache_options": {"mode": "explicit"}}
            )
        )

        errors = readiness_errors(cfg, ["global-standard"])

        self.assertIn(
            "generation.extra_params cannot override runner-controlled keys: "
            "prompt_cache_options",
            errors,
        )

    def test_process_watchdog_kills_worker_at_hard_limit(self):
        class FakeProcess:
            def __init__(self):
                self.alive = True
                self.exitcode = None
                self.killed = False
                self.join_timeouts = []

            def start(self):
                pass

            def join(self, timeout):
                self.join_timeouts.append(timeout)

            def is_alive(self):
                return self.alive

            def kill(self):
                self.killed = True
                self.alive = False
                self.exitcode = -9

        process = FakeProcess()
        context = SimpleNamespace(Process=lambda **kwargs: process)
        cfg = SimpleNamespace(max_run_duration_s=2.0, shutdown_grace_s=0.5)

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            with patch("app.multiprocessing.get_context", return_value=context):
                exit_code = run_with_process_watchdog(cfg, [], "run", out_dir)
            stopped = json.loads((out_dir / "stopped.json").read_text())

        self.assertEqual(exit_code, 124)
        self.assertTrue(process.killed)
        self.assertEqual(process.join_timeouts, [2.0, 1])
        self.assertTrue(stopped["forced"])

    def test_readiness_rejects_empty_and_nonpositive_matrix_values(self):
        cfg = canonical_config()
        cfg = replace(
            cfg,
            workloads=(),
            concurrency_levels=(0,),
            offered_load_rpm=(0.0,),
            streaming_load_rpm=(float("nan"),),
        )

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertIn("workloads must contain at least one workload", errors)
        self.assertIn(
            "concurrency_levels[0] must be finite and greater than 0", errors
        )
        self.assertIn(
            "offered_load_rpm[0] must be finite and greater than 0", errors
        )
        self.assertIn(
            "streaming_load_rpm[0] must be finite and greater than 0", errors
        )

    def test_readiness_rejects_null_metadata_and_nonobject_skus(self):
        cfg = canonical_config()
        cfg = replace(
            cfg,
            api_version_verified="true",
            deployments={
                "global-standard": "same-deployment",
                "provisioned": "same-deployment",
            },
            experiment={
                **cfg.experiment,
                "client_location": None,
                "deployment_skus": {
                    "global-standard": {
                        "name": "GlobalStandard",
                        "capacity": 1350,
                    },
                    "provisioned": {
                        "name": "GlobalStandard",
                        "capacity": 45,
                    },
                },
            },
        )

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertIn(
            "experiment.client_location must be a nonempty string", errors
        )
        self.assertIn(
            "api_version_verified must be true after live API discovery", errors
        )
        self.assertIn("selected deployment names must be distinct", errors)
        self.assertIn("selected deployment SKU names must be distinct", errors)

        nonobject_cfg = replace(
            cfg,
            experiment={**cfg.experiment, "deployment_skus": "not-an-object"},
        )
        self.assertIn(
            "experiment.deployment_skus must be an object",
            readiness_errors(nonobject_cfg, list(nonobject_cfg.deployments)),
        )


if __name__ == "__main__":
    unittest.main()