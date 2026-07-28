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
import platform
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

RUNNER_VERSION = "1.0.0"

SYSTEM_MESSAGE = (
    "You are a benchmarking assistant. Answer the user's request directly and "
    "continuously until you reach the length limit. Do not ask questions."
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


@dataclass(frozen=True)
class Workload:
    name: str
    input_tokens: int
    max_output_tokens: int

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Workload":
        return Workload(
            name=d["name"],
            input_tokens=int(d["input_tokens"]),
            max_output_tokens=int(d["max_output_tokens"]),
        )


@dataclass(frozen=True)
class Config:
    endpoint: str
    api_version: str
    deployments: dict[str, str]
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

    @staticmethod
    def load(path: Path) -> "Config":
        raw = json.loads(path.read_text())
        deployments = raw["deployments"]
        if not deployments:
            raise ValueError("config.deployments must not be empty")
        return Config(
            endpoint=raw["endpoint"].rstrip("/"),
            api_version=raw["api_version"],
            deployments=dict(deployments),
            workloads=tuple(Workload.from_dict(w) for w in raw["workloads"]),
            seed=int(raw.get("seed", 0)),
            trials=int(raw.get("trials", 3)),
            trial_duration_s=float(raw.get("trial_duration_s", 60)),
            warmup_requests=int(raw.get("warmup_requests", 5)),
            concurrency_levels=tuple(int(c) for c in raw.get("concurrency_levels", [1, 2, 4, 8, 16, 32])),
            offered_load_rpm=tuple(float(r) for r in raw.get("offered_load_rpm", [])),
            streaming_load_rpm=tuple(float(r) for r in raw.get("streaming_load_rpm", [])),
            connect_timeout_s=float(raw.get("timeouts", {}).get("connect_s", 10)),
            read_timeout_s=float(raw.get("timeouts", {}).get("read_s", 180)),
            max_in_flight=int(raw.get("max_in_flight", 512)),
            token_param=raw.get("generation", {}).get("token_param", "max_completion_tokens"),
            extra_generation_params=dict(raw.get("generation", {}).get("extra_params", {})),
            output_dir=Path(raw.get("output_dir", "results")),
            inter_trial_pause_s=float(raw.get("inter_trial_pause_s", 3.0)),
        )


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
        repeats = math.ceil(budget / max(1, len(self._enc.encode(_FILLER)))) + 1
        tokens = self._enc.encode(_FILLER * repeats)[:budget]
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
    inter_token_latency_s: float | None

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
            "model": deployment,
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": self._prompts.build(wl.input_tokens)},
            ],
            self._cfg.token_param: wl.max_output_tokens,
            **self._cfg.extra_generation_params,
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
    ) -> RequestResult:
        wl = scenario.workload
        stream = scenario.mode == "stream"
        kwargs = self._kwargs(deployment_name, wl, stream)

        start_epoch = time.time()
        t0 = time.perf_counter()
        queue_delay = None if intended_start_epoch is None else max(0.0, start_epoch - intended_start_epoch)

        throttled = False
        status = "ok"
        http_status: int | None = None
        error_type: str | None = None
        error_message: str | None = None

        ttft: float | None = None
        stream_complete: float | None = None
        inter_token: float | None = None
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
                    inter_token,
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
            inter_token_latency_s=inter_token,
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
        if getattr(resp, "choices", None):
            finish = getattr(resp.choices[0], "finish_reason", None)
        return (
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "total_tokens", None),
            finish,
        )

    async def _stream_once(self, kwargs: dict[str, Any], attempt_t0: float):
        ttft: float | None = None
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
                chunks += 1
                if ttft is None:
                    ttft = time.perf_counter() - attempt_t0

        complete = time.perf_counter() - attempt_t0
        inter_token = None
        if ttft is not None and chunks > 1:
            inter_token = (complete - ttft) / (chunks - 1)

        return (
            ttft,
            complete,
            inter_token,
            chunks,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "total_tokens", None),
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
) -> list[RequestResult]:
    """Fixed number of active clients; each worker issues requests back to back."""
    results: list[RequestResult] = []
    deadline = time.perf_counter() + duration_s
    counter = 0

    async def worker(worker_id: int) -> None:
        nonlocal counter
        while time.perf_counter() < deadline:
            counter += 1
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
            results.append(res)

    await asyncio.gather(*(worker(i) for i in range(scenario.concurrency or 1)))
    return results


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
) -> tuple[list[RequestResult], int]:
    """Fixed arrival rate independent of completion; reveals queue growth."""
    results: list[RequestResult] = []
    rpm = scenario.offered_rpm or 1.0
    mean_gap = 60.0 / rpm

    sem = asyncio.Semaphore(max_in_flight)
    tasks: list[asyncio.Task[None]] = []
    start = time.perf_counter()
    start_epoch = time.time()
    offset = 0.0
    seq = 0
    in_flight = 0
    peak_in_flight = 0

    async def issue(intended_epoch: float, seq_no: int) -> None:
        nonlocal in_flight, peak_in_flight
        async with sem:
            in_flight += 1
            if in_flight > peak_in_flight:
                peak_in_flight = in_flight
            try:
                res = await executor.run(
                    scenario=scenario,
                    deployment_label=deployment_label,
                    deployment_name=deployment_name,
                    trial=trial,
                    worker_id=-1,
                    seq=seq_no,
                    run_id=run_id,
                    intended_start_epoch=intended_epoch,
                )
                results.append(res)
            finally:
                in_flight -= 1

    while offset < duration_s:
        offset += rng.expovariate(1.0 / mean_gap)
        if offset >= duration_s:
            break
        seq += 1
        sleep_for = (start + offset) - time.perf_counter()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        tasks.append(asyncio.create_task(issue(start_epoch + offset, seq)))

    if tasks:
        await asyncio.gather(*tasks)
    return results, peak_in_flight


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


def aggregate(results: Iterable[RequestResult], duration_s: float, *, peak_in_flight: int | None = None) -> dict[str, Any]:
    rows = list(results)
    if not rows:
        return {"requests": 0}

    ok = [r for r in rows if r.status == "ok"]
    lat = [r.total_latency_s for r in ok]
    ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
    itl = [r.inter_token_latency_s for r in ok if r.inter_token_latency_s is not None]
    queue = [r.queue_delay_s for r in rows if r.queue_delay_s is not None]

    prompt_tokens = sum(r.prompt_tokens or 0 for r in ok)
    completion_tokens = sum(r.completion_tokens or 0 for r in ok)

    n = len(rows)
    return {
        "requests": n,
        "successful": len(ok),
        "duration_s": round(duration_s, 3),
        "success_rps": round(len(ok) / duration_s, 4) if duration_s else None,
        "achieved_rpm": round(n / duration_s * 60, 2) if duration_s else None,
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
        },
        "inter_token_latency_s": {"p50": percentile(itl, 50), "p95": percentile(itl, 95)},
        "queue_delay_s": {"p50": percentile(queue, 50), "p95": percentile(queue, 95), "max": max(queue) if queue else None},
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "prompt_per_s": round(prompt_tokens / duration_s, 2) if duration_s else None,
            "completion_per_s": round(completion_tokens / duration_s, 2) if duration_s else None,
            "total_per_s": round((prompt_tokens + completion_tokens) / duration_s, 2) if duration_s else None,
        },
        "rates": {
            "throttled_429": round(sum(1 for r in rows if r.throttled) / n, 4),
            "timeout": round(sum(1 for r in rows if r.status == "timeout") / n, 4),
            "http_error": round(sum(1 for r in rows if r.status == "http_error") / n, 4),
            "exception": round(sum(1 for r in rows if r.status == "exception") / n, 4),
        },
        "peak_in_flight": peak_in_flight,
        "sample_warning": (
            "fewer than 100 successful samples; p95/p99 are not reliable"
            if len(ok) < 100
            else None
        ),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_client(cfg: Config):
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
    from openai import AsyncAzureOpenAI
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
    client = AsyncAzureOpenAI(
        azure_endpoint=cfg.endpoint,
        azure_ad_token_provider=token_provider,
        api_version=cfg.api_version,
        # Retries are accounted for explicitly by Executor; the SDK must not
        # silently absorb 429s or the throttling result would be invisible.
        max_retries=0,
        http_client=http_client,
    )
    return client, credential, http_client


async def warm_up(executor: Executor, cfg: Config, scenario: Scenario, label: str, name: str, run_id: str) -> None:
    """Excluded from results: primes connections, DNS, TLS, and the deployment."""
    for i in range(cfg.warmup_requests):
        await executor.run(
            scenario=scenario,
            deployment_label=label,
            deployment_name=name,
            trial=-1,
            worker_id=-99,
            seq=i,
            run_id=run_id,
            intended_start_epoch=None,
        )


async def execute(cfg: Config, labels: list[str], run_id: str, out_dir: Path) -> None:
    scenarios = build_scenarios(cfg)
    prompts = PromptFactory()
    client, credential, http_client = build_client(cfg)
    executor = Executor(client, cfg, prompts)

    raw_path = out_dir / "requests.jsonl"
    agg: list[dict[str, Any]] = []

    try:
        with raw_path.open("w", encoding="utf-8") as raw:
            warmed: set[tuple[str, str]] = set()

            for trial in range(cfg.trials):
                order = deployment_order(labels, trial)
                rng = random.Random(cfg.seed + trial)
                shuffled = list(scenarios)
                rng.shuffle(shuffled)

                for label in order:
                    name = cfg.deployments[label]
                    for scenario in shuffled:
                        key = (label, scenario.workload.name)
                        if key not in warmed:
                            print(f"  warm-up {label}/{scenario.workload.name}", flush=True)
                            await warm_up(executor, cfg, scenario, label, name, run_id)
                            warmed.add(key)

                        print(
                            f"  trial {trial} | {label:<16} | {scenario.scenario_id}",
                            flush=True,
                        )
                        t0 = time.perf_counter()
                        if scenario.pass_name == "concurrency":
                            rows = await run_closed_loop(
                                executor,
                                scenario,
                                deployment_label=label,
                                deployment_name=name,
                                trial=trial,
                                run_id=run_id,
                                duration_s=cfg.trial_duration_s,
                            )
                            peak = scenario.concurrency
                        else:
                            scenario_hash = int(hashlib.sha256(scenario.scenario_id.encode()).hexdigest(), 16) % 1000
                            rows, peak = await run_open_loop(
                                executor,
                                scenario,
                                deployment_label=label,
                                deployment_name=name,
                                trial=trial,
                                run_id=run_id,
                                duration_s=cfg.trial_duration_s,
                                rng=random.Random(cfg.seed + trial * 1000 + scenario_hash),
                                max_in_flight=cfg.max_in_flight,
                            )
                        elapsed = time.perf_counter() - t0

                        for r in rows:
                            raw.write(r.to_json() + "\n")
                        raw.flush()

                        summary = aggregate(rows, elapsed, peak_in_flight=peak)
                        summary.update(
                            {
                                "run_id": run_id,
                                "trial": trial,
                                "deployment_label": label,
                                "deployment_name": name,
                                "scenario_id": scenario.scenario_id,
                                "pass": scenario.pass_name,
                                "workload": scenario.workload.name,
                                "mode": scenario.mode,
                                "concurrency": scenario.concurrency,
                                "offered_rpm": scenario.offered_rpm,
                            }
                        )
                        agg.append(summary)
                        await asyncio.sleep(cfg.inter_trial_pause_s)
    finally:
        await client.close()
        await http_client.aclose()
        await credential.close()

    (out_dir / "aggregates.json").write_text(json.dumps(agg, indent=2))
    print(f"\nwrote {raw_path}")
    print(f"wrote {out_dir / 'aggregates.json'}")


def write_manifest(cfg: Config, labels: list[str], run_id: str, out_dir: Path, config_path: Path) -> None:
    """Immutable record of everything the experiment holds constant."""
    manifest = {
        "run_id": run_id,
        "runner_version": RUNNER_VERSION,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "config_file": str(config_path),
        "config": {
            "endpoint": cfg.endpoint,
            "api_version": cfg.api_version,
            "deployments": {k: cfg.deployments[k] for k in labels},
            "seed": cfg.seed,
            "trials": cfg.trials,
            "trial_duration_s": cfg.trial_duration_s,
            "warmup_requests": cfg.warmup_requests,
            "concurrency_levels": list(cfg.concurrency_levels),
            "offered_load_rpm": list(cfg.offered_load_rpm),
            "streaming_load_rpm": list(cfg.streaming_load_rpm),
            "token_param": cfg.token_param,
            "extra_generation_params": cfg.extra_generation_params,
            "timeouts": {"connect_s": cfg.connect_timeout_s, "read_s": cfg.read_timeout_s},
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
    }
    try:
        import openai
        import azure.identity

        manifest["client"]["openai"] = openai.__version__
        manifest["client"]["azure_identity"] = azure.identity.__version__
    except Exception:  # pragma: no cover
        pass

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def print_matrix(cfg: Config, labels: list[str]) -> None:
    scenarios = build_scenarios(cfg)
    runs = len(scenarios) * cfg.trials * len(labels)
    est_s = runs * (cfg.trial_duration_s + cfg.inter_trial_pause_s)

    print(f"deployments      : {', '.join(f'{k} -> {cfg.deployments[k]}' for k in labels)}")
    print(f"workloads        : {', '.join(w.name for w in cfg.workloads)}")
    print(f"scenarios        : {len(scenarios)}")
    print(f"trials           : {cfg.trials}")
    print(f"total runs       : {runs}")
    print(f"trial duration   : {cfg.trial_duration_s:g}s (+{cfg.inter_trial_pause_s:g}s pause)")
    print(f"estimated wall   : {est_s / 60:.1f} min ({est_s / 3600:.2f} h), excluding warm-up")
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

    cfg = Config.load(args.config)
    labels = list(args.only) if args.only else list(cfg.deployments)
    unknown = [l for l in labels if l not in cfg.deployments]
    if unknown:
        print(f"unknown deployment label(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(cfg.deployments)}", file=sys.stderr)
        return 2

    print_matrix(cfg, labels)

    if args.dry_run:
        print("\ndry run: no network calls made, no resources touched")
        return 0

    unresolved = [l for l in labels if not cfg.deployments[l] or cfg.deployments[l].startswith("<")]
    if unresolved:
        print(
            f"\nrefusing to run: deployment name unset for {', '.join(unresolved)}",
            file=sys.stderr,
        )
        return 2

    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    out_dir = args.output_dir or (cfg.output_dir / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(cfg, labels, run_id, out_dir, args.config)
    print(f"\nrun_id: {run_id}\noutput: {out_dir}\n")

    asyncio.run(execute(cfg, labels, run_id, out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
