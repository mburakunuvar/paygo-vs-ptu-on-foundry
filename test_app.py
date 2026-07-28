import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    BenchmarkDeadlineExceeded,
    Config,
    Executor,
    LoadStats,
    RequestResult,
    Scenario,
    WarmupError,
    Workload,
    aggregate,
    build_scenarios,
    build_warmup_scenario,
    execute,
    execute_with_deadline,
    main,
    nominal_runtime_s,
    readiness_errors,
    run_with_process_watchdog,
    run_closed_loop,
    run_open_loop,
    warm_up,
    write_manifest,
)


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
        prompts = SimpleNamespace(build=lambda tokens: "prompt")
        executor = Executor(SimpleNamespace(), cfg, prompts)

        kwargs = executor._kwargs(
            "expected-deployment", Workload("rag", 1000, 300), False
        )

        self.assertEqual(kwargs["model"], "expected-deployment")
        self.assertEqual(kwargs["max_completion_tokens"], 300)
        self.assertEqual(kwargs["messages"][0]["role"], "system")
        self.assertEqual(kwargs["temperature"], 0.2)

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
            SimpleNamespace(build=lambda tokens: "prompt"),
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
            SimpleNamespace(build=lambda tokens: "prompt"),
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
    def test_config_rejects_lossy_numeric_coercion(self):
        original = json.loads(Path("bench.config.json").read_text())
        invalid_values = (
            ("trials", True),
            ("warmup_requests", 1.9),
            ("max_in_flight", True),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for field, value in invalid_values:
                with self.subTest(field=field):
                    raw = dict(original)
                    raw[field] = value
                    path.write_text(json.dumps(raw))
                    with self.assertRaisesRegex(ValueError, field):
                        Config.load(path)

    def test_manifest_records_reproducibility_contract(self):
        cfg = Config.load(Path("bench.config.json"))
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            external_config = out_dir / "external-config.json"
            external_config.write_text(Path("bench.config.json").read_text())
            write_manifest(
                cfg,
                ["global-standard"],
                "run",
                out_dir,
                external_config,
                {"file": "pip-freeze.txt", "sha256": "digest"},
            )
            manifest = json.loads((out_dir / "manifest.json").read_text())

        self.assertIn("source", manifest)
        self.assertEqual(len(manifest["source"]["commit"]), 40)
        self.assertEqual(len(manifest["runner_sha256"]), 64)
        self.assertEqual(len(manifest["config_sha256"]), 64)
        self.assertIn("experiment", manifest)
        self.assertEqual(manifest["retry_policy"]["attempts_per_request"], 1)
        self.assertEqual(manifest["config"]["max_run_duration_s"], 8700)
        self.assertEqual(manifest["config"]["max_in_flight"], 64)
        self.assertEqual(manifest["dependency_snapshot"]["sha256"], "digest")

    def test_current_full_matrix_fits_with_operational_slack(self):
        cfg = Config.load(Path("bench.config.json"))

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertEqual(len(build_scenarios(cfg)), 24)
        self.assertEqual(nominal_runtime_s(cfg, list(cfg.deployments)), 7632)
        self.assertFalse(any("nominal matrix time" in error for error in errors))

    def test_readiness_requires_baseline_and_target_load_coverage(self):
        cfg = Config.load(Path("bench.config.json"))
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

    def test_dry_run_reports_readiness_errors_without_network_calls(self):
        output = io.StringIO()
        errors = io.StringIO()
        raw_config = json.loads(Path("bench.config.json").read_text())
        raw_config["deployments"]["provisioned"] = "<unresolved>"

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "bench.config.json"
            config_path.write_text(json.dumps(raw_config))
            with (
                patch.object(
                    sys,
                    "argv",
                    ["app.py", "--config", str(config_path), "--dry-run"],
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

    def test_readiness_rejects_zero_warmup_and_incomplete_sku_metadata(self):
        cfg = Config.load(Path("bench.config.json"))
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

    def test_readiness_requires_three_trials_and_rejects_reserved_extras(self):
        cfg = Config.load(Path("bench.config.json"))
        cfg = replace(
            cfg,
            trials=1,
            extra_generation_params={"model": "wrong-deployment"},
        )

        errors = readiness_errors(cfg, list(cfg.deployments))

        self.assertIn("trials must be at least 3", errors)
        self.assertTrue(
            any("runner-controlled keys: model" in error for error in errors)
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
        cfg = Config.load(Path("bench.config.json"))
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
        cfg = Config.load(Path("bench.config.json"))
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
                        "capacity": 30,
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