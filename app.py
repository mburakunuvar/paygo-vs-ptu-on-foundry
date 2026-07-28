#!/usr/bin/env python3
"""Benchmark runner: Global Standard (pay-as-you-go) vs. provisioned throughput.

Implements section 8 of 03-ptuVSpaygo.md. Deployment names are configuration,
not code, so the same binary runs the validation phase (pay-as-you-go only) and
the measurement phase (both deployments) without touching a code path for the
first time while provisioned capacity is billing.

Authentication is Microsoft Entra ID only. No API keys are read or written.

Usage:
    ./.venv/bin/python app.py --config bench.config.json --dry-run
    ./.venv/bin/python app.py --config bench.config.json --only global-standard
    ./.venv/bin/python app.py --config bench.config.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import multiprocessing
import platform
import random
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

RUNNER_VERSION = "1.2.0"

SYSTEM_MESSAGE = (
    "You are a benchmarking assistant. Answer the user's request directly and "
    "continuously until you reach the length limit. Do not ask questions."
)

_PROMPT_PREFIX = (
    "Explain the capacity-planning scenario below in clear technical prose. "
    "Discuss load, queueing, latency, and throughput without asking questions. "
)

# Deterministic filler used to build prompts of an exact token length.
_FILLER = (
    "The distribution network routes each request through a regional gateway "
    "before the scheduler assigns it to an available worker. Capacity planning "
    "depends on arrival rate, service time, and the variance of both. When "
    "offered load approaches the service ceiling, queueing delay grows without "
    "bound and tail latency separates sharply from the median. "
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _config_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    return value


def _config_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    return float(value)


def _config_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return value


@dataclass(frozen=True)
class Workload:
    name: str
    input_tokens: int
    max_output_tokens: int

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Workload":
        if not isinstance(d, dict):
            raise ValueError("each workload must be an object")
        return Workload(
            name=d["name"],
            input_tokens=_config_int(d["input_tokens"], "workload.input_tokens"),
            max_output_tokens=_config_int(
                d["max_output_tokens"], "workload.max_output_tokens"
            ),
        )


@dataclass(frozen=True)
class Config:
    endpoint: str
    api_version: str
    api_version_verified: bool
    deployments: dict[str, str]
    experiment: dict[str, Any]
    workloads: tuple[Workload, ...]
    seed: int
    trials: int
    trial_duration_s: float
    warmup_requests: int
    concurrency_levels: tuple[int, ...]
    offered_load_rpm: tuple[float, ...]
    streaming_load_rpm: tuple[float, ...]
    connect_timeout_s: float
    read_timeout_s: float
    max_in_flight: int
    token_param: str
    extra_generation_params: dict[str, Any]
    output_dir: Path
    inter_trial_pause_s: float
    max_run_duration_s: float
    shutdown_grace_s: float

    @staticmethod
    def load(path: Path) -> "Config":
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
        deployments = raw["deployments"]
        if not isinstance(deployments, dict) or not deployments:
            raise ValueError("config.deployments must be a nonempty object")
        if any(not isinstance(name, str) for name in deployments.values()):
            raise ValueError("config deployment names must be strings")
        workloads = _config_list(raw["workloads"], "workloads")
        concurrency_levels = _config_list(
            raw.get("concurrency_levels", [1, 2, 4, 8, 16, 32]),
            "concurrency_levels",
        )
        offered_load_rpm = _config_list(
            raw.get("offered_load_rpm", []), "offered_load_rpm"
        )
        streaming_load_rpm = _config_list(
            raw.get("streaming_load_rpm", []), "streaming_load_rpm"
        )
        timeouts = raw.get("timeouts", {})
        if not isinstance(timeouts, dict):
            raise ValueError("timeouts must be an object")
        generation = raw.get("generation", {})
        if not isinstance(generation, dict):
            raise ValueError("generation must be an object")
        extra_params = generation.get("extra_params", {})
        if not isinstance(extra_params, dict):
            raise ValueError("generation.extra_params must be an object")
        experiment = raw.get("experiment", {})
        if not isinstance(experiment, dict):
            raise ValueError("experiment must be an object")
        return Config(
            endpoint=raw["endpoint"].rstrip("/"),
            api_version=raw["api_version"],
            api_version_verified=raw.get("api_version_verified", False),
            deployments=dict(deployments),
            experiment=dict(experiment),
            workloads=tuple(Workload.from_dict(w) for w in workloads),
            seed=_config_int(raw.get("seed", 0), "seed"),
            trials=_config_int(raw.get("trials", 3), "trials"),
            trial_duration_s=_config_number(
                raw.get("trial_duration_s", 60), "trial_duration_s"
            ),
            warmup_requests=_config_int(
                raw.get("warmup_requests", 5), "warmup_requests"
            ),
            concurrency_levels=tuple(
                _config_int(value, f"concurrency_levels[{index}]")
                for index, value in enumerate(concurrency_levels)
            ),
            offered_load_rpm=tuple(
                _config_number(value, f"offered_load_rpm[{index}]")
                for index, value in enumerate(offered_load_rpm)
            ),
            streaming_load_rpm=tuple(
                _config_number(value, f"streaming_load_rpm[{index}]")
                for index, value in enumerate(streaming_load_rpm)
            ),
            connect_timeout_s=_config_number(
                timeouts.get("connect_s", 10), "timeouts.connect_s"
            ),
            read_timeout_s=_config_number(
                timeouts.get("read_s", 180), "timeouts.read_s"
            ),
            max_in_flight=_config_int(
                raw.get("max_in_flight", 512), "max_in_flight"
            ),
            token_param=generation.get("token_param", "max_completion_tokens"),
            extra_generation_params=dict(extra_params),
            output_dir=Path(raw.get("output_dir", "results")),
            inter_trial_pause_s=_config_number(
                raw.get("inter_trial_pause_s", 3.0), "inter_trial_pause_s"
            ),
            max_run_duration_s=_config_number(
                raw.get("max_run_duration_s", 8700), "max_run_duration_s"
            ),
            shutdown_grace_s=_config_number(
                raw.get("shutdown_grace_s", 10), "shutdown_grace_s"
            ),
        )


class WarmupError(RuntimeError):
    pass


class BenchmarkDeadlineExceeded(RuntimeError):
    pass


class InvalidResponseError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Scenario matrix
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    pass_name: str          # concurrency | offered_load | ttft
    workload: Workload
    mode: str               # stream | nonstream
    concurrency: int | None
    offered_rpm: float | None


@dataclass
class LoadStats:
    peak_in_flight: int | None = None
    peak_client_backlog: int | None = None
    arrival_window_s: float | None = None
    scheduled_requests: int | None = None


@dataclass
class ActiveTrial:
    run_id: str
    trial: int
    deployment_label: str
    deployment_name: str
    scenario: Scenario
    rows: list[RequestResult]
    stats: LoadStats
    started_epoch: float
    started_perf: float


def build_scenarios(cfg: Config) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for wl in cfg.workloads:
        for c in cfg.concurrency_levels:
            scenarios.append(
                Scenario(
                    scenario_id=f"conc-{wl.name}-c{c}",
                    pass_name="concurrency",
                    workload=wl,
                    mode="nonstream",
                    concurrency=c,
                    offered_rpm=None,
                )
            )
        for rpm in cfg.offered_load_rpm:
            scenarios.append(
                Scenario(
                    scenario_id=f"load-{wl.name}-r{rpm:g}",
                    pass_name="offered_load",
                    workload=wl,
                    mode="nonstream",
                    concurrency=None,
                    offered_rpm=rpm,
                )
            )
        for rpm in cfg.streaming_load_rpm:
            scenarios.append(
                Scenario(
                    scenario_id=f"ttft-{wl.name}-r{rpm:g}",
                    pass_name="ttft",
                    workload=wl,
                    mode="stream",
                    concurrency=None,
                    offered_rpm=rpm,
                )
            )
    return scenarios


def build_warmup_scenario(workload: Workload) -> Scenario:
    return Scenario(
        scenario_id=f"warmup-{workload.name}",
        pass_name="warmup",
        workload=workload,
        mode="nonstream",
        concurrency=1,
        offered_rpm=None,
    )


def deployment_order(labels: Sequence[str], trial: int) -> list[str]:
    """Alternate deployment order between trials to cancel drift and ordering bias."""
    ordered = list(labels)
    if trial % 2 == 1:
        ordered.reverse()
    return ordered


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


class PromptFactory:
    """Builds prompts of an exact token length, deterministically."""

    def __init__(self, model_hint: str = "o200k_base") -> None:
        import tiktoken

        try:
            self._enc = tiktoken.get_encoding(model_hint)
        except Exception:
            self._enc = tiktoken.get_encoding("cl100k_base")
        self._system_tokens = len(self._enc.encode(SYSTEM_MESSAGE))
        self._cache: dict[int, str] = {}

    def build(self, target_input_tokens: int) -> str:
        """Return user text so that system + user is close to target_input_tokens."""
        if target_input_tokens in self._cache:
            return self._cache[target_input_tokens]

        budget = max(16, target_input_tokens - self._system_tokens)
        filler_tokens = max(1, len(self._enc.encode(_FILLER)))
        repeats = math.ceil(budget / filler_tokens) + 1
        tokens = self._enc.encode(_PROMPT_PREFIX + _FILLER * repeats)[:budget]
        text = self._enc.decode(tokens)
        self._cache[target_input_tokens] = text
        return text


# --------------------------------------------------------------------------
# Request execution
# --------------------------------------------------------------------------


@dataclass
class RequestResult:
    run_id: str
    scenario_id: str
    pass_name: str
    deployment_label: str
    deployment_name: str
    workload: str
    mode: str
    trial: int
    concurrency: int | None
    offered_rpm: float | None
    worker_id: int
    seq: int

    intended_start_epoch: float | None
    start_epoch: float
    end_epoch: float
    queue_delay_s: float | None

    total_latency_s: float
    ttft_s: float | None
    stream_complete_s: float | None
    mean_output_token_interval_s: float | None

    status: str
    http_status: int | None
    error_type: str | None
    error_message: str | None

    throttled: bool

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    stream_chunks: int | None
    finish_reason: str | None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))


def _classify(exc: BaseException) -> tuple[str, int | None, bool]:
    """Return (status, http_status, is_throttle)."""
    if isinstance(exc, InvalidResponseError):
        return "invalid_response", 200, False

    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)

    name = type(exc).__name__
    if status_code == 429:
        return "throttled", 429, True
    if isinstance(exc, asyncio.TimeoutError) or "Timeout" in name:
        return "timeout", status_code, False
    if status_code is not None:
        return "http_error", int(status_code), False
    return "exception", None, False


class Executor:
    """Issues one request and records timing, tokens, and failure mode."""

    def __init__(self, client: Any, cfg: Config, prompts: PromptFactory) -> None:
        self._client = client
        self._cfg = cfg
        self._prompts = prompts

    def _kwargs(self, deployment: str, wl: Workload, stream: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            **self._cfg.extra_generation_params,
            "model": deployment,
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": self._prompts.build(wl.input_tokens)},
            ],
            self._cfg.token_param: wl.max_output_tokens,
        }
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    async def run(
        self,
        *,
        scenario: Scenario,
        deployment_label: str,
        deployment_name: str,
        trial: int,
        worker_id: int,
        seq: int,
        run_id: str,
        intended_start_epoch: float | None,
        intended_start_perf: float | None = None,
    ) -> RequestResult:
        wl = scenario.workload
        stream = scenario.mode == "stream"
        kwargs = self._kwargs(deployment_name, wl, stream)

        start_epoch = time.time()
        t0 = time.perf_counter()
        queue_delay = (
            None
            if intended_start_perf is None
            else max(0.0, t0 - intended_start_perf)
        )

        throttled = False
        status = "ok"
        http_status: int | None = None
        error_type: str | None = None
        error_message: str | None = None

        ttft: float | None = None
        stream_complete: float | None = None
        mean_output_token_interval: float | None = None
        chunks: int | None = None
        prompt_tokens = completion_tokens = total_tokens = None
        finish_reason: str | None = None

        # Exactly one attempt: the SDK's own retries are disabled and the runner
        # does not retry either, so a 429 is recorded as a result rather than
        # being hidden behind a backoff.
        try:
            if stream:
                (
                    ttft,
                    stream_complete,
                    mean_output_token_interval,
                    chunks,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    finish_reason,
                ) = await self._stream_once(kwargs, t0)
            else:
                (
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    finish_reason,
                ) = await self._complete_once(kwargs)
            http_status = 200
        except Exception as exc:  # noqa: BLE001 - benchmark records every failure mode
            status, http_status, throttled = _classify(exc)
            error_type = type(exc).__name__
            error_message = str(exc)[:400]

        total_latency = time.perf_counter() - t0
        return RequestResult(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            pass_name=scenario.pass_name,
            deployment_label=deployment_label,
            deployment_name=deployment_name,
            workload=wl.name,
            mode=scenario.mode,
            trial=trial,
            concurrency=scenario.concurrency,
            offered_rpm=scenario.offered_rpm,
            worker_id=worker_id,
            seq=seq,
            intended_start_epoch=intended_start_epoch,
            start_epoch=start_epoch,
            end_epoch=time.time(),
            queue_delay_s=queue_delay,
            total_latency_s=total_latency,
            ttft_s=ttft,
            stream_complete_s=stream_complete,
            mean_output_token_interval_s=mean_output_token_interval,
            status=status,
            http_status=http_status,
            error_type=error_type,
            error_message=error_message,
            throttled=throttled,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            stream_chunks=chunks,
            finish_reason=finish_reason,
        )

    async def _complete_once(self, kwargs: dict[str, Any]):
        resp = await self._client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        finish = None
        choices = getattr(resp, "choices", None)
        if choices:
            finish = getattr(choices[0], "finish_reason", None)
        content = (
            getattr(getattr(choices[0], "message", None), "content", None)
            if choices
            else None
        )
        if not isinstance(content, str) or not content.strip():
            raise InvalidResponseError("response contained no generated text")

        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if not all(
            isinstance(value, int)
            for value in (prompt_tokens, completion_tokens, total_tokens)
        ):
            raise InvalidResponseError("response contained incomplete token usage")
        return (
            prompt_tokens,
            completion_tokens,
            total_tokens,
            finish,
        )

    async def _stream_once(self, kwargs: dict[str, Any], attempt_t0: float):
        ttft: float | None = None
        last_content_s: float | None = None
        chunks = 0
        finish: str | None = None
        usage = None

        stream = await self._client.chat.completions.create(**kwargs)
        async for event in stream:
            usage = getattr(event, "usage", None) or usage
            choices = getattr(event, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta else None
            finish = getattr(choices[0], "finish_reason", None) or finish
            # TTFT is the first content-bearing event, not a role-only event.
            if content:
                last_content_s = time.perf_counter() - attempt_t0
                chunks += 1
                if ttft is None:
                    ttft = last_content_s

        complete = time.perf_counter() - attempt_t0
        if ttft is None:
            raise InvalidResponseError("stream contained no generated text")
        completion_tokens = getattr(usage, "completion_tokens", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if not all(
            isinstance(value, int)
            for value in (prompt_tokens, completion_tokens, total_tokens)
        ):
            raise InvalidResponseError("stream contained incomplete token usage")
        mean_output_token_interval = None
        if (
            ttft is not None
            and last_content_s is not None
            and chunks > 1
            and last_content_s > ttft
            and completion_tokens is not None
            and completion_tokens > 1
        ):
            mean_output_token_interval = (
                last_content_s - ttft
            ) / (completion_tokens - 1)

        return (
            ttft,
            complete,
            mean_output_token_interval,
            chunks,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            finish,
        )


# --------------------------------------------------------------------------
# Load generators
# --------------------------------------------------------------------------


async def run_closed_loop(
    executor: Executor,
    scenario: Scenario,
    *,
    deployment_label: str,
    deployment_name: str,
    trial: int,
    run_id: str,
    duration_s: float,
    results: list[RequestResult] | None = None,
    on_result: Callable[[RequestResult], None] | None = None,
    stats: LoadStats | None = None,
) -> list[RequestResult]:
    """Fixed number of active clients; each worker issues requests back to back."""
    collected = results if results is not None else []
    load_stats = stats if stats is not None else LoadStats()
    load_stats.peak_in_flight = scenario.concurrency or 1
    load_stats.scheduled_requests = 0
    start = time.perf_counter()
    deadline = start + duration_s
    counter = 0
    scheduling_complete = False

    async def worker(worker_id: int) -> None:
        nonlocal counter
        while time.perf_counter() < deadline:
            counter += 1
            load_stats.scheduled_requests = counter
            seq = counter
            res = await executor.run(
                scenario=scenario,
                deployment_label=deployment_label,
                deployment_name=deployment_name,
                trial=trial,
                worker_id=worker_id,
                seq=seq,
                run_id=run_id,
                intended_start_epoch=None,
            )
            collected.append(res)
            if on_result is not None:
                on_result(res)

    tasks = [
        asyncio.create_task(worker(i))
        for i in range(scenario.concurrency or 1)
    ]
    try:
        await asyncio.gather(*tasks)
        scheduling_complete = True
    finally:
        load_stats.arrival_window_s = (
            duration_s
            if scheduling_complete
            else min(duration_s, max(0.0, time.perf_counter() - start))
        )
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return collected


async def run_open_loop(
    executor: Executor,
    scenario: Scenario,
    *,
    deployment_label: str,
    deployment_name: str,
    trial: int,
    run_id: str,
    duration_s: float,
    rng: random.Random,
    max_in_flight: int,
    results: list[RequestResult] | None = None,
    on_result: Callable[[RequestResult], None] | None = None,
    stats: LoadStats | None = None,
) -> tuple[list[RequestResult], int, int]:
    """Fixed arrival rate independent of completion; reveals queue growth."""
    collected = results if results is not None else []
    load_stats = stats if stats is not None else LoadStats()
    load_stats.peak_in_flight = 0
    load_stats.peak_client_backlog = 0
    load_stats.scheduled_requests = 0
    rpm = scenario.offered_rpm or 1.0
    mean_gap = 60.0 / rpm

    arrivals: asyncio.Queue[tuple[float, float, int] | None] = asyncio.Queue()
    start = time.perf_counter()
    start_epoch = time.time()
    offset = 0.0
    seq = 0
    in_flight = 0
    peak_in_flight = 0
    peak_client_backlog = 0
    scheduling_complete = False

    async def worker(worker_id: int) -> None:
        nonlocal in_flight, peak_in_flight
        while True:
            arrival = await arrivals.get()
            try:
                if arrival is None:
                    return
                intended_epoch, intended_perf, seq_no = arrival
                in_flight += 1
                if in_flight > peak_in_flight:
                    peak_in_flight = in_flight
                    load_stats.peak_in_flight = peak_in_flight
                try:
                    res = await executor.run(
                        scenario=scenario,
                        deployment_label=deployment_label,
                        deployment_name=deployment_name,
                        trial=trial,
                        worker_id=worker_id,
                        seq=seq_no,
                        run_id=run_id,
                        intended_start_epoch=intended_epoch,
                        intended_start_perf=intended_perf,
                    )
                    collected.append(res)
                    if on_result is not None:
                        on_result(res)
                finally:
                    in_flight -= 1
            finally:
                arrivals.task_done()

    try:
        async with asyncio.TaskGroup() as workers:
            for worker_id in range(max_in_flight):
                workers.create_task(worker(worker_id))

            while offset < duration_s:
                offset += rng.expovariate(1.0 / mean_gap)
                if offset >= duration_s:
                    remaining_s = (start + duration_s) - time.perf_counter()
                    if remaining_s > 0:
                        await asyncio.sleep(remaining_s)
                    break
                seq += 1
                sleep_for = (start + offset) - time.perf_counter()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                else:
                    await asyncio.sleep(0)
                arrivals.put_nowait((start_epoch + offset, start + offset, seq))
                load_stats.scheduled_requests = seq
                peak_client_backlog = max(
                    peak_client_backlog, arrivals.qsize()
                )
                load_stats.peak_client_backlog = peak_client_backlog

            scheduling_complete = True
            load_stats.arrival_window_s = duration_s
            await arrivals.join()
            for _ in range(max_in_flight):
                arrivals.put_nowait(None)
    finally:
        if not scheduling_complete:
            load_stats.arrival_window_s = min(
                duration_s, max(0.0, time.perf_counter() - start)
            )
    return collected, peak_in_flight, peak_client_backlog


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def aggregate(
    results: Iterable[RequestResult],
    elapsed_s: float,
    *,
    arrival_window_s: float | None = None,
    peak_in_flight: int | None = None,
    peak_client_backlog: int | None = None,
    scheduled_requests: int | None = None,
) -> dict[str, Any]:
    rows = list(results)
    ok = [r for r in rows if r.status == "ok"]
    lat = [r.total_latency_s for r in ok]
    ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
    stream_completions = [
        r.stream_complete_s
        for r in ok
        if r.stream_complete_s is not None
    ]
    output_intervals = [
        r.mean_output_token_interval_s
        for r in ok
        if r.mean_output_token_interval_s is not None
    ]
    queue = [r.queue_delay_s for r in rows if r.queue_delay_s is not None]

    prompt_tokens = sum(r.prompt_tokens or 0 for r in ok)
    completion_tokens = sum(r.completion_tokens or 0 for r in ok)

    n = len(rows)
    arrival_count = scheduled_requests if scheduled_requests is not None else n
    rate_window_s = arrival_window_s if arrival_window_s is not None else elapsed_s

    def error_rate(count: int) -> float | None:
        return round(count / n, 4) if n else None

    return {
        "requests": n,
        "scheduled_requests": scheduled_requests,
        "uncompleted_scheduled_requests": (
            max(0, scheduled_requests - n)
            if scheduled_requests is not None
            else None
        ),
        "successful": len(ok),
        "elapsed_s": round(elapsed_s, 3),
        "arrival_window_s": round(arrival_window_s, 3) if arrival_window_s is not None else None,
        "success_rps": round(len(ok) / elapsed_s, 4) if elapsed_s else None,
        "achieved_arrival_rpm": (
            round(arrival_count / rate_window_s * 60, 2)
            if rate_window_s
            else None
        ),
        "latency_s": {
            "p50": percentile(lat, 50),
            "p90": percentile(lat, 90),
            "p95": percentile(lat, 95),
            "p99": percentile(lat, 99),
            "max": max(lat) if lat else None,
        },
        "ttft_s": {
            "p50": percentile(ttfts, 50),
            "p90": percentile(ttfts, 90),
            "p95": percentile(ttfts, 95),
            "p99": percentile(ttfts, 99),
            "samples": len(ttfts),
            "sample_warning": (
                "fewer than 100 usable TTFT samples; p95/p99 are not reliable"
                if len(ttfts) < 100
                else None
            ),
        },
        "stream_completion_s": {
            "p50": percentile(stream_completions, 50),
            "p90": percentile(stream_completions, 90),
            "p95": percentile(stream_completions, 95),
            "p99": percentile(stream_completions, 99),
            "samples": len(stream_completions),
            "sample_warning": (
                "fewer than 100 usable stream samples; p95/p99 are not reliable"
                if len(stream_completions) < 100
                else None
            ),
        },
        "mean_output_token_interval_s": {
            "p50": percentile(output_intervals, 50),
            "p95": percentile(output_intervals, 95),
            "samples": len(output_intervals),
            "sample_warning": (
                "fewer than 100 usable cadence samples; p95 is not reliable"
                if len(output_intervals) < 100
                else None
            ),
        },
        "queue_delay_s": {"p50": percentile(queue, 50), "p95": percentile(queue, 95), "max": max(queue) if queue else None},
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "prompt_per_s": round(prompt_tokens / elapsed_s, 2) if elapsed_s else None,
            "completion_per_s": round(completion_tokens / elapsed_s, 2) if elapsed_s else None,
            "total_per_s": round((prompt_tokens + completion_tokens) / elapsed_s, 2) if elapsed_s else None,
        },
        "rates": {
            "throttled_429": error_rate(sum(1 for r in rows if r.throttled)),
            "timeout": error_rate(sum(1 for r in rows if r.status == "timeout")),
            "other_error": error_rate(
                sum(
                    1
                    for r in rows
                    if r.status not in {"ok", "throttled", "timeout"}
                )
            ),
            "http_error": error_rate(
                sum(1 for r in rows if r.status == "http_error")
            ),
            "exception": error_rate(
                sum(1 for r in rows if r.status == "exception")
            ),
            "invalid_response": error_rate(
                sum(1 for r in rows if r.status == "invalid_response")
            ),
        },
        "peak_in_flight": peak_in_flight,
        "peak_client_backlog": peak_client_backlog,
        "sample_warning": (
            "fewer than 100 successful samples; p95/p99 are not reliable"
            if len(ok) < 100
            else None
        ),
    }


def summarize_trial(
    active: ActiveTrial,
    ended_epoch: float,
    ended_perf: float,
    *,
    partial: bool,
) -> dict[str, Any]:
    scenario = active.scenario
    summary = aggregate(
        active.rows,
        max(0.0, ended_perf - active.started_perf),
        arrival_window_s=active.stats.arrival_window_s,
        peak_in_flight=active.stats.peak_in_flight,
        peak_client_backlog=active.stats.peak_client_backlog,
        scheduled_requests=active.stats.scheduled_requests,
    )
    summary.update(
        {
            "run_id": active.run_id,
            "trial": active.trial,
            "deployment_label": active.deployment_label,
            "deployment_name": active.deployment_name,
            "scenario_id": scenario.scenario_id,
            "pass": scenario.pass_name,
            "workload": scenario.workload.name,
            "mode": scenario.mode,
            "concurrency": scenario.concurrency,
            "offered_rpm": scenario.offered_rpm,
            "started_utc": datetime.fromtimestamp(
                active.started_epoch, timezone.utc
            ).isoformat(),
            "ended_utc": datetime.fromtimestamp(ended_epoch, timezone.utc).isoformat(),
            "partial": partial,
        }
    )
    return summary


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def write_json_atomic(path: Path, value: Any) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def build_client(cfg: Config):
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
    from openai import AsyncOpenAI
    import httpx

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout_s,
            read=cfg.read_timeout_s,
            write=cfg.read_timeout_s,
            pool=cfg.connect_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=max(64, cfg.max_in_flight),
            max_keepalive_connections=max(64, cfg.max_in_flight),
        ),
    )
    client = AsyncOpenAI(
        base_url=f"{cfg.endpoint.rstrip('/')}/openai/{cfg.api_version}/",
        api_key=token_provider,
        # No retries anywhere: the runner issues exactly one attempt per request
        # and the SDK must not silently absorb 429s, or throttling would be
        # invisible in the results.
        max_retries=0,
        http_client=http_client,
    )
    return client, credential, http_client


async def warm_up(executor: Executor, cfg: Config, scenario: Scenario, label: str, name: str, run_id: str) -> None:
    """Excluded from results: primes connections, DNS, TLS, and the deployment."""
    for i in range(cfg.warmup_requests):
        result = await executor.run(
            scenario=scenario,
            deployment_label=label,
            deployment_name=name,
            trial=-1,
            worker_id=-99,
            seq=i,
            run_id=run_id,
            intended_start_epoch=None,
        )
        if result.status != "ok":
            detail = result.error_message or result.status
            raise WarmupError(
                f"warm-up failed for {label}/{scenario.workload.name}: "
                f"{result.status} ({result.http_status}): {detail}"
            )


async def execute(cfg: Config, labels: list[str], run_id: str, out_dir: Path) -> None:
    scenarios = build_scenarios(cfg)
    prompts = PromptFactory()
    client, credential, http_client = build_client(cfg)
    executor = Executor(client, cfg, prompts)

    raw_path = out_dir / "requests.jsonl"
    aggregates_path = out_dir / "aggregates.json"
    agg: list[dict[str, Any]] = []
    active: ActiveTrial | None = None
    write_json_atomic(aggregates_path, agg)

    try:
        with raw_path.open("w", encoding="utf-8") as raw:
            def persist_result(result: RequestResult) -> None:
                raw.write(result.to_json() + "\n")
                raw.flush()

            for label in labels:
                name = cfg.deployments[label]
                for workload in cfg.workloads:
                    print(f"  warm-up {label}/{workload.name}", flush=True)
                    await warm_up(
                        executor,
                        cfg,
                        build_warmup_scenario(workload),
                        label,
                        name,
                        run_id,
                    )

            for trial in range(cfg.trials):
                order = deployment_order(labels, trial)
                rng = random.Random(cfg.seed + trial)
                shuffled = list(scenarios)
                rng.shuffle(shuffled)

                for label in order:
                    name = cfg.deployments[label]
                    for scenario in shuffled:
                        print(
                            f"  trial {trial} | {label:<16} | {scenario.scenario_id}",
                            flush=True,
                        )
                        trial_started_epoch = time.time()
                        t0 = time.perf_counter()
                        stats = LoadStats()
                        active = ActiveTrial(
                            run_id=run_id,
                            trial=trial,
                            deployment_label=label,
                            deployment_name=name,
                            scenario=scenario,
                            rows=[],
                            stats=stats,
                            started_epoch=trial_started_epoch,
                            started_perf=t0,
                        )
                        if scenario.pass_name == "concurrency":
                            await run_closed_loop(
                                executor,
                                scenario,
                                deployment_label=label,
                                deployment_name=name,
                                trial=trial,
                                run_id=run_id,
                                duration_s=cfg.trial_duration_s,
                                results=active.rows,
                                on_result=persist_result,
                                stats=stats,
                            )
                        else:
                            scenario_hash = int(hashlib.sha256(scenario.scenario_id.encode()).hexdigest(), 16) % 1000
                            await run_open_loop(
                                executor,
                                scenario,
                                deployment_label=label,
                                deployment_name=name,
                                trial=trial,
                                run_id=run_id,
                                duration_s=cfg.trial_duration_s,
                                rng=random.Random(cfg.seed + trial * 1000 + scenario_hash),
                                max_in_flight=cfg.max_in_flight,
                                results=active.rows,
                                on_result=persist_result,
                                stats=stats,
                            )
                        trial_ended_epoch = time.time()
                        agg.append(
                            summarize_trial(
                                active,
                                trial_ended_epoch,
                                time.perf_counter(),
                                partial=False,
                            )
                        )
                        write_json_atomic(aggregates_path, agg)
                        active = None
                        await asyncio.sleep(cfg.inter_trial_pause_s)
    finally:
        if active is not None:
            agg.append(
                summarize_trial(
                    active,
                    time.time(),
                    time.perf_counter(),
                    partial=True,
                )
            )
        write_json_atomic(aggregates_path, agg)
        print(f"\nwrote {raw_path}")
        print(f"wrote {aggregates_path}")
        await client.close()
        await http_client.aclose()
        await credential.close()


async def execute_with_deadline(
    cfg: Config,
    labels: list[str],
    run_id: str,
    out_dir: Path,
    timeout_s: float | None = None,
) -> None:
    deadline_s = cfg.max_run_duration_s if timeout_s is None else timeout_s
    try:
        async with asyncio.timeout(deadline_s):
            await execute(cfg, labels, run_id, out_dir)
    except TimeoutError as exc:
        raise BenchmarkDeadlineExceeded(
            f"benchmark reached its {deadline_s / 60:g}-minute execution "
            "wall-clock limit; partial results were preserved"
        ) from exc


def _write_stop_record(out_dir: Path, reason: str, *, forced: bool) -> None:
    (out_dir / "stopped.json").write_text(
        json.dumps(
            {
                "stopped_utc": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "forced": forced,
            },
            indent=2,
        )
    )


def _benchmark_worker(
    cfg: Config,
    labels: list[str],
    run_id: str,
    out_dir: Path,
    execution_budget_s: float,
) -> None:
    try:
        asyncio.run(
            execute_with_deadline(
                cfg,
                labels,
                run_id,
                out_dir,
                timeout_s=execution_budget_s,
            )
        )
    except (WarmupError, BenchmarkDeadlineExceeded) as exc:
        _write_stop_record(out_dir, str(exc), forced=False)
        print(f"benchmark stopped: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


def run_with_process_watchdog(
    cfg: Config, labels: list[str], run_id: str, out_dir: Path
) -> int:
    execution_budget_s = cfg.max_run_duration_s - cfg.shutdown_grace_s
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_benchmark_worker,
        args=(cfg, labels, run_id, out_dir, execution_budget_s),
        name="benchmark-worker",
    )
    process.start()
    try:
        process.join(cfg.max_run_duration_s)
    except KeyboardInterrupt:
        if process.is_alive():
            process.kill()
            process.join(1)
        _write_stop_record(out_dir, "runner interrupted by operator", forced=True)
        return 130

    if process.is_alive():
        reason = (
            f"benchmark worker exceeded the hard {cfg.max_run_duration_s / 60:g}-"
            "minute wall-clock limit and was killed"
        )
        _write_stop_record(out_dir, reason, forced=True)
        process.kill()
        process.join(1)
        print(f"benchmark stopped: {reason}", file=sys.stderr, flush=True)
        return 124
    return process.exitcode if process.exitcode is not None else 1


def source_revision(repo_dir: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    try:
        return {
            "commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def write_dependency_snapshot(out_dir: Path) -> dict[str, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        check=True,
        capture_output=True,
        text=True,
    )
    content = completed.stdout.rstrip() + "\n"
    path = out_dir / "pip-freeze.txt"
    path.write_text(content)
    return {
        "file": path.name,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def write_manifest(
    cfg: Config,
    labels: list[str],
    run_id: str,
    out_dir: Path,
    config_path: Path,
    dependency_snapshot: dict[str, str],
) -> None:
    """Immutable record of everything the experiment holds constant."""
    runner_path = Path(__file__).resolve()
    resolved_config_path = config_path.resolve()
    manifest = {
        "run_id": run_id,
        "runner_version": RUNNER_VERSION,
        "runner_sha256": hashlib.sha256(runner_path.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(
            resolved_config_path.read_bytes()
        ).hexdigest(),
        "source": source_revision(runner_path.parent),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": cfg.experiment,
        "config_file": str(config_path),
        "config": {
            "endpoint": cfg.endpoint,
            "api_version": cfg.api_version,
            "api_version_verified": cfg.api_version_verified,
            "deployments": {k: cfg.deployments[k] for k in labels},
            "seed": cfg.seed,
            "trials": cfg.trials,
            "trial_duration_s": cfg.trial_duration_s,
            "inter_trial_pause_s": cfg.inter_trial_pause_s,
            "max_run_duration_s": cfg.max_run_duration_s,
            "shutdown_grace_s": cfg.shutdown_grace_s,
            "warmup_requests": cfg.warmup_requests,
            "concurrency_levels": list(cfg.concurrency_levels),
            "offered_load_rpm": list(cfg.offered_load_rpm),
            "streaming_load_rpm": list(cfg.streaming_load_rpm),
            "token_param": cfg.token_param,
            "extra_generation_params": cfg.extra_generation_params,
            "timeouts": {"connect_s": cfg.connect_timeout_s, "read_s": cfg.read_timeout_s},
            "max_in_flight": cfg.max_in_flight,
            "workloads": [
                {"name": w.name, "input_tokens": w.input_tokens, "max_output_tokens": w.max_output_tokens}
                for w in cfg.workloads
            ],
        },
        "system_message": SYSTEM_MESSAGE,
        "client": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": sys.executable,
        },
        "auth": "DefaultAzureCredential (Entra ID); no API keys used",
        "retry_policy": {
            "attempts_per_request": 1,
            "sdk_max_retries": 0,
        },
        "dependency_snapshot": dependency_snapshot,
    }
    try:
        import openai
        import azure.identity

        manifest["client"]["openai"] = openai.__version__
        manifest["client"]["azure_identity"] = azure.identity.__version__
    except Exception:  # pragma: no cover
        pass

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def nominal_runtime_s(cfg: Config, labels: Sequence[str]) -> float:
    return (
        len(build_scenarios(cfg))
        * cfg.trials
        * len(labels)
        * (cfg.trial_duration_s + cfg.inter_trial_pause_s)
    )


def _unresolved_paths(value: Any, prefix: str) -> list[str]:
    if value is None:
        return [prefix]
    if isinstance(value, str):
        return [prefix] if not value.strip() or value.lstrip().startswith("<") else []
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            paths.extend(_unresolved_paths(child, f"{prefix}.{key}"))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_unresolved_paths(child, f"{prefix}[{index}]"))
        return paths
    return []


def readiness_errors(cfg: Config, labels: Sequence[str]) -> list[str]:
    errors: list[str] = []
    if (
        not isinstance(cfg.endpoint, str)
        or not cfg.endpoint.strip()
        or cfg.endpoint.lstrip().startswith("<")
    ):
        errors.append("endpoint is unresolved")
    if (
        not isinstance(cfg.api_version, str)
        or not cfg.api_version.strip()
        or cfg.api_version.lstrip().startswith("<")
    ):
        errors.append("api_version is unresolved")
    elif cfg.api_version != "v1":
        errors.append("api_version must be v1 for the configured OpenAI client")
    for label in labels:
        name = cfg.deployments[label]
        if (
            not isinstance(name, str)
            or not name.strip()
            or name.lstrip().startswith("<")
        ):
            errors.append(f"deployment name is unresolved for {label}")
    if cfg.api_version_verified is not True:
        errors.append("api_version_verified must be true after live API discovery")

    required_string_metadata = (
        "model_name",
        "model_version",
        "model_format",
        "region",
        "client_location",
        "content_filter_policy",
        "version_upgrade_policy",
    )
    for key in required_string_metadata:
        value = cfg.experiment.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"experiment.{key} must be a nonempty string")
        elif value.lstrip().startswith("<"):
            errors.append(f"experiment.{key} is unresolved")

    errors.extend(
        f"{path} is unresolved"
        for path in _unresolved_paths(cfg.experiment, "experiment")
        if path not in {f"experiment.{key}" for key in required_string_metadata}
    )

    if cfg.warmup_requests < 1:
        errors.append("warmup_requests must be at least 1")
    if cfg.trials < 3:
        errors.append("trials must be at least 3")
    if cfg.max_in_flight < 1:
        errors.append("max_in_flight must be at least 1")
    if not math.isfinite(cfg.trial_duration_s) or cfg.trial_duration_s <= 0:
        errors.append("trial_duration_s must be greater than 0")
    if not math.isfinite(cfg.inter_trial_pause_s) or cfg.inter_trial_pause_s < 0:
        errors.append("inter_trial_pause_s must be nonnegative")
    if not math.isfinite(cfg.connect_timeout_s) or cfg.connect_timeout_s <= 0:
        errors.append("timeouts.connect_s must be greater than 0")
    if not math.isfinite(cfg.read_timeout_s) or cfg.read_timeout_s <= 0:
        errors.append("timeouts.read_s must be greater than 0")
    if not math.isfinite(cfg.max_run_duration_s) or cfg.max_run_duration_s <= 0:
        errors.append("max_run_duration_s must be greater than 0")
    if not math.isfinite(cfg.shutdown_grace_s) or cfg.shutdown_grace_s <= 0:
        errors.append("shutdown_grace_s must be greater than 0")
    elif cfg.shutdown_grace_s >= cfg.max_run_duration_s:
        errors.append("shutdown_grace_s must be less than max_run_duration_s")

    if not cfg.workloads:
        errors.append("workloads must contain at least one workload")
    seen_workloads: set[str] = set()
    for index, workload in enumerate(cfg.workloads):
        if (
            not isinstance(workload.name, str)
            or not workload.name.strip()
            or workload.name.lstrip().startswith("<")
        ):
            errors.append(f"workloads[{index}].name must be a nonempty string")
        else:
            if workload.name in seen_workloads:
                errors.append(f"workloads[{index}].name must be unique")
            seen_workloads.add(workload.name)
        if workload.input_tokens <= 0:
            errors.append(f"workloads[{index}].input_tokens must be greater than 0")
        if workload.max_output_tokens <= 0:
            errors.append(
                f"workloads[{index}].max_output_tokens must be greater than 0"
            )

    load_levels = (
        ("concurrency_levels", cfg.concurrency_levels),
        ("offered_load_rpm", cfg.offered_load_rpm),
        ("streaming_load_rpm", cfg.streaming_load_rpm),
    )
    for field, values in load_levels:
        if not values:
            errors.append(f"{field} must contain at least one value")
        if len(set(values)) != len(values):
            errors.append(f"{field} values must be unique")
        for index, value in enumerate(values):
            if not math.isfinite(value) or value <= 0:
                errors.append(f"{field}[{index}] must be finite and greater than 0")

    if 1 not in cfg.concurrency_levels:
        errors.append("concurrency_levels must include baseline level 1")

    target_rpm = cfg.experiment.get("target_rpm")
    if (
        isinstance(target_rpm, bool)
        or not isinstance(target_rpm, (int, float))
        or not math.isfinite(target_rpm)
        or target_rpm <= 0
    ):
        errors.append("experiment.target_rpm must be finite and greater than 0")
    else:
        target = float(target_rpm)

        def includes(levels: Sequence[float], relation: str) -> bool:
            finite = [level for level in levels if math.isfinite(level)]
            if relation == "below":
                return any(level < target for level in finite)
            if relation == "above":
                return any(level > target for level in finite)
            return any(math.isclose(level, target) for level in finite)

        required_coverage = (
            ("offered_load_rpm", cfg.offered_load_rpm, "below"),
            ("offered_load_rpm", cfg.offered_load_rpm, "target"),
            ("offered_load_rpm", cfg.offered_load_rpm, "above"),
            ("streaming_load_rpm", cfg.streaming_load_rpm, "below"),
            ("streaming_load_rpm", cfg.streaming_load_rpm, "target"),
        )
        for field, levels, relation in required_coverage:
            if not includes(levels, relation):
                errors.append(
                    f"{field} must include a level {relation} "
                    "experiment.target_rpm"
                )

    controlled_request_keys = {
        "model",
        "messages",
        "stream",
        "stream_options",
        cfg.token_param,
    }
    overridden_keys = controlled_request_keys.intersection(
        cfg.extra_generation_params
    )
    if overridden_keys:
        errors.append(
            "generation.extra_params cannot override runner-controlled keys: "
            + ", ".join(sorted(overridden_keys))
        )
    if (
        not isinstance(cfg.token_param, str)
        or not cfg.token_param.strip()
        or cfg.token_param in {"model", "messages", "stream", "stream_options"}
    ):
        errors.append("generation.token_param is invalid")

    deployment_skus = cfg.experiment.get("deployment_skus")
    if not isinstance(deployment_skus, dict):
        errors.append("experiment.deployment_skus must be an object")
    else:
        selected_sku_names: list[str] = []
        for label in labels:
            sku = deployment_skus.get(label)
            if not isinstance(sku, dict):
                errors.append(f"experiment.deployment_skus.{label} is missing")
                continue
            if (
                not isinstance(sku.get("name"), str)
                or not sku["name"].strip()
                or sku["name"].lstrip().startswith("<")
            ):
                errors.append(
                    f"experiment.deployment_skus.{label}.name is unresolved"
                )
            else:
                selected_sku_names.append(sku["name"])
            capacity = sku.get("capacity")
            if (
                isinstance(capacity, bool)
                or not isinstance(capacity, (int, float))
                or not math.isfinite(capacity)
                or capacity <= 0
            ):
                errors.append(
                    f"experiment.deployment_skus.{label}.capacity must be greater than 0"
                )
        if len(selected_sku_names) != len(set(selected_sku_names)):
            errors.append("selected deployment SKU names must be distinct")

    selected_deployment_names = [cfg.deployments[label] for label in labels]
    valid_deployment_names = [
        name
        for name in selected_deployment_names
        if isinstance(name, str) and name.strip() and not name.lstrip().startswith("<")
    ]
    if len(valid_deployment_names) != len(set(valid_deployment_names)):
        errors.append("selected deployment names must be distinct")

    nominal_s = nominal_runtime_s(cfg, labels)
    execution_budget_s = cfg.max_run_duration_s - cfg.shutdown_grace_s
    if nominal_s >= execution_budget_s:
        errors.append(
            f"nominal matrix time ({nominal_s / 60:.1f} min) leaves no room "
            f"inside the execution budget ({execution_budget_s / 60:.1f} min) "
            "for warm-up or request drain"
        )
    return errors


def print_matrix(cfg: Config, labels: list[str]) -> None:
    scenarios = build_scenarios(cfg)
    runs = len(scenarios) * cfg.trials * len(labels)
    est_s = nominal_runtime_s(cfg, labels)

    print(f"deployments      : {', '.join(f'{k} -> {cfg.deployments[k]}' for k in labels)}")
    print(f"workloads        : {', '.join(w.name for w in cfg.workloads)}")
    print(f"scenarios        : {len(scenarios)}")
    print(f"trials           : {cfg.trials}")
    print(f"total runs       : {runs}")
    print(f"trial duration   : {cfg.trial_duration_s:g}s (+{cfg.inter_trial_pause_s:g}s pause)")
    print(f"estimated wall   : {est_s / 60:.1f} min ({est_s / 3600:.2f} h), excluding warm-up")
    print(f"wall-time limit  : {cfg.max_run_duration_s / 60:.1f} min, including warm-up and drain")
    print(f"shutdown grace   : {cfg.shutdown_grace_s:g}s before the hard limit")
    print(f"nominal slack    : {(cfg.max_run_duration_s - cfg.shutdown_grace_s - est_s) / 60:.1f} min")
    print(f"seed             : {cfg.seed}")
    print()
    print(f"{'scenario_id':<34} {'pass':<13} {'mode':<10} {'conc':>5} {'rpm':>7}")
    print("-" * 74)
    for s in scenarios:
        print(
            f"{s.scenario_id:<34} {s.pass_name:<13} {s.mode:<10} "
            f"{s.concurrency if s.concurrency is not None else '-':>5} "
            f"{f'{s.offered_rpm:g}' if s.offered_rpm is not None else '-':>7}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Global Standard vs. provisioned throughput benchmark")
    ap.add_argument("--config", type=Path, default=Path("bench.config.json"))
    ap.add_argument("--dry-run", action="store_true", help="print the matrix and exit; contacts nothing")
    ap.add_argument("--only", action="append", default=None, help="restrict to a deployment label (repeatable)")
    ap.add_argument("--output-dir", type=Path, default=None)
    args = ap.parse_args()

    if not args.config.exists():
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2

    try:
        cfg = Config.load(args.config)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    labels = list(args.only) if args.only else list(cfg.deployments)
    unknown = [l for l in labels if l not in cfg.deployments]
    if unknown:
        print(f"unknown deployment label(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(cfg.deployments)}", file=sys.stderr)
        return 2

    print_matrix(cfg, labels)

    errors = readiness_errors(cfg, labels)

    if args.dry_run:
        print("\ndry run: no network calls made, no resources touched")
        if errors:
            print("\ndry-run readiness failed:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 2
        return 0

    if errors:
        print("\nrefusing to run:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 2

    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    out_dir = args.output_dir or (cfg.output_dir / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        dependency_snapshot = write_dependency_snapshot(out_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"failed to capture dependency snapshot: {exc}", file=sys.stderr)
        return 2
    write_manifest(cfg, labels, run_id, out_dir, args.config, dependency_snapshot)
    print(f"\nrun_id: {run_id}\noutput: {out_dir}\n")

    return run_with_process_watchdog(cfg, labels, run_id, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
