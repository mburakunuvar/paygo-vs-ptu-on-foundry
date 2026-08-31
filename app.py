#!/usr/bin/env python3
"""Benchmark runner: Global Standard (pay-as-you-go) vs. provisioned throughput.

Deployment names are configuration, not code, so the same runner handles both
the pay-as-you-go validation phase and the full comparison documented in
README.md.

Authentication is Microsoft Entra ID only. No API keys are read or written.

Usage:
    python app.py --dry-run
    python app.py --only global-standard
    python app.py

Configuration comes from the environment. Copy .env.example to .env and fill it
in, or export the same variables directly; real environment variables take
precedence over .env entries.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import multiprocessing
import os
import platform
import random
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

RUNNER_VERSION = "1.4.0"

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


def _config_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return value


DEPLOYMENT_ENV_PREFIX = "AZURE_DEPLOYMENT_"
SKU_ENV_PREFIX = "BENCH_SKU_"
DEFAULT_DEPLOYMENT_LABELS = ("global-standard", "provisioned")
AZURE_OPENAI_HOST_SUFFIX = ".openai.azure.com"
ALLOWED_TOKEN_PARAMS = {"max_completion_tokens", "max_tokens"}
PROMPT_CACHE_CONTROL_KEYS = {
    "prompt_cache_key",
    "prompt_cache_options",
    "prompt_cache_retention",
}
SENSITIVE_KEY_MARKERS = {
    "accesstoken",
    "accountkey",
    "apikey",
    "authorization",
    "connectionstring",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "sharedaccesssignature",
}

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<quote>[\"']?)\b(?P<key>"
    r"authorization|api[-_ ]?key|account[-_ ]?key|client[-_ ]?secret|"
    r"access[-_ ]?token|refresh[-_ ]?token|connection[-_ ]?string|"
    r"password|private[-_ ]?key|shared[-_ ]?access[-_ ]?signature|sig"
    r")(?P=quote)(?P<separator>\s*[:=]\s*)"
    r"(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def _parse_int(raw: str, path: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{path} must be an integer") from None


def _parse_float(raw: str, path: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{path} must be a number") from None


def _parse_scalar_number(raw: str, path: str) -> int | float:
    """Keep whole numbers as ints so manifests read 30 rather than 30.0."""
    try:
        return int(raw)
    except ValueError:
        return _parse_float(raw, path)


def _env_text(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    return raw.strip() or None


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _env_text(env, name)
    return default if raw is None else _parse_int(raw, name)


def _env_number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _env_text(env, name)
    return default if raw is None else _parse_float(raw, name)


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _env_text(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_json(env: Mapping[str, str], name: str, default: Any) -> Any:
    raw = _env_text(env, name)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc.msg}") from None


def _env_items(env: Mapping[str, str], name: str) -> list[str] | None:
    raw = _env_text(env, name)
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_ints(
    env: Mapping[str, str], name: str, default: Sequence[int]
) -> list[int]:
    items = _env_items(env, name)
    if items is None:
        return list(default)
    return [_parse_int(item, f"{name}[{i}]") for i, item in enumerate(items)]


def _env_numbers(
    env: Mapping[str, str], name: str, default: Sequence[float]
) -> list[float]:
    items = _env_items(env, name)
    if items is None:
        return list(default)
    return [_parse_float(item, f"{name}[{i}]") for i, item in enumerate(items)]


def _env_label(suffix: str) -> str:
    return suffix.lower().replace("_", "-")


def _label_suffix(label: str) -> str:
    return label.upper().replace("-", "_")


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    return normalized == "sig" or any(
        marker in normalized for marker in SENSITIVE_KEY_MARKERS
    )


def _sensitive_key_paths(value: Any, prefix: str) -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if _is_sensitive_key(key):
                paths.append(path)
            else:
                paths.extend(_sensitive_key_paths(child, path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_sensitive_key_paths(child, f"{prefix}[{index}]"))
        return paths
    return []


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if _is_sensitive_key(key)
                else _redact_sensitive_values(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(child) for child in value]
    return value


def _endpoint_validation_error(endpoint: str) -> str | None:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return "endpoint must be a valid URL"

    if parsed.scheme.lower() != "https":
        return "endpoint must use HTTPS"
    if parsed.username is not None or parsed.password is not None:
        return "endpoint must not include credentials"

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not (
        hostname.endswith(AZURE_OPENAI_HOST_SUFFIX)
        and hostname != AZURE_OPENAI_HOST_SUFFIX.removeprefix(".")
    ):
        return "endpoint host is not a supported public Azure OpenAI domain"
    if port not in (None, 443):
        return "endpoint must use the default HTTPS port"
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return "endpoint must be a resource root without a path, query, or fragment"
    return None


def _safe_error_message(exc: BaseException) -> str:
    message = _PRIVATE_KEY_RE.sub("<redacted private key>", str(exc))
    message = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('quote')}{match.group('key')}"
            f"{match.group('quote')}{match.group('separator')}<redacted>"
        ),
        message,
    )
    message = _BEARER_TOKEN_RE.sub("Bearer <redacted>", message)
    message = "".join(
        character
        if ord(character) >= 32 and ord(character) != 127
        else " "
        for character in message
    )
    return " ".join(message.split())[:400]


@dataclass(frozen=True)
class Workload:
    name: str
    input_tokens: int
    max_output_tokens: int

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Workload":
        if not isinstance(d, dict):
            raise ValueError("each workload must be an object")
        required_fields = {"name", "input_tokens", "max_output_tokens"}
        missing_fields = sorted(required_fields.difference(d))
        if missing_fields:
            raise ValueError(
                "workload is missing required field(s): "
                + ", ".join(missing_fields)
            )
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
    unset_env_vars: tuple[str, ...] = ()

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        unset: list[str] = []

        def required(name: str) -> str:
            value = _env_text(env, name)
            if value is not None:
                return value
            unset.append(name)
            return f"<unset {name}>"

        endpoint = required("AZURE_OPENAI_ENDPOINT").rstrip("/")

        deployments: dict[str, str] = {}
        for key in sorted(env):
            if not key.startswith(DEPLOYMENT_ENV_PREFIX):
                continue
            value = _env_text(env, key)
            if value is not None:
                deployments[_env_label(key[len(DEPLOYMENT_ENV_PREFIX) :])] = value
        if not deployments:
            for label in DEFAULT_DEPLOYMENT_LABELS:
                deployments[label] = required(
                    f"{DEPLOYMENT_ENV_PREFIX}{_label_suffix(label)}"
                )

        experiment: dict[str, Any] = {
            "subscription_id": required("AZURE_SUBSCRIPTION_ID"),
            "tenant_id": required("AZURE_TENANT_ID"),
            "resource_group": required("AZURE_RESOURCE_GROUP"),
            "foundry_resource": required("AZURE_FOUNDRY_RESOURCE"),
            "foundry_project": required("AZURE_FOUNDRY_PROJECT"),
            "model_name": required("BENCH_MODEL_NAME"),
            "model_version": required("BENCH_MODEL_VERSION"),
            "model_format": required("BENCH_MODEL_FORMAT"),
            "region": required("BENCH_REGION"),
            "client_location": required("BENCH_CLIENT_LOCATION"),
            "content_filter_policy": required("BENCH_CONTENT_FILTER_POLICY"),
            "version_upgrade_policy": required("BENCH_VERSION_UPGRADE_POLICY"),
            "routing_scope": required("BENCH_ROUTING_SCOPE"),
        }

        # Absent keys, rather than "<unset ...>" markers, keep readiness from
        # reporting the same field twice for numeric and SKU metadata.
        target_rpm = _env_text(env, "BENCH_TARGET_RPM")
        if target_rpm is None:
            unset.append("BENCH_TARGET_RPM")
        else:
            experiment["target_rpm"] = _parse_scalar_number(
                target_rpm, "BENCH_TARGET_RPM"
            )

        skus: dict[str, dict[str, Any]] = {}
        for label in deployments:
            prefix = f"{SKU_ENV_PREFIX}{_label_suffix(label)}"
            sku: dict[str, Any] = {}
            sku_name = _env_text(env, f"{prefix}_NAME")
            if sku_name is None:
                unset.append(f"{prefix}_NAME")
            else:
                sku["name"] = sku_name
            capacity = _env_text(env, f"{prefix}_CAPACITY")
            if capacity is None:
                unset.append(f"{prefix}_CAPACITY")
            else:
                sku["capacity"] = _parse_scalar_number(
                    capacity, f"{prefix}_CAPACITY"
                )
            skus[label] = sku
        experiment["deployment_skus"] = skus

        raw_workloads = _env_json(env, "BENCH_WORKLOADS", None)
        if raw_workloads is None:
            unset.append("BENCH_WORKLOADS")
            raw_workloads = []
        workloads = _config_list(raw_workloads, "BENCH_WORKLOADS")

        extra_params = _env_json(env, "BENCH_GENERATION_EXTRA_PARAMS", {})
        if not isinstance(extra_params, dict):
            raise ValueError("BENCH_GENERATION_EXTRA_PARAMS must be a JSON object")

        return Config(
            endpoint=endpoint,
            api_version=_env_text(env, "AZURE_OPENAI_API_VERSION") or "v1",
            api_version_verified=_env_bool(
                env, "AZURE_OPENAI_API_VERSION_VERIFIED", False
            ),
            deployments=deployments,
            experiment=experiment,
            workloads=tuple(Workload.from_dict(w) for w in workloads),
            seed=_env_int(env, "BENCH_SEED", 0),
            trials=_env_int(env, "BENCH_TRIALS", 3),
            trial_duration_s=_env_number(env, "BENCH_TRIAL_DURATION_S", 60),
            warmup_requests=_env_int(env, "BENCH_WARMUP_REQUESTS", 5),
            concurrency_levels=tuple(
                _env_ints(
                    env, "BENCH_CONCURRENCY_LEVELS", (1, 2, 4, 8, 16, 32)
                )
            ),
            offered_load_rpm=tuple(_env_numbers(env, "BENCH_OFFERED_LOAD_RPM", ())),
            streaming_load_rpm=tuple(
                _env_numbers(env, "BENCH_STREAMING_LOAD_RPM", ())
            ),
            connect_timeout_s=_env_number(env, "BENCH_CONNECT_TIMEOUT_S", 10),
            read_timeout_s=_env_number(env, "BENCH_READ_TIMEOUT_S", 180),
            max_in_flight=_env_int(env, "BENCH_MAX_IN_FLIGHT", 512),
            token_param=_env_text(env, "BENCH_TOKEN_PARAM")
            or "max_completion_tokens",
            extra_generation_params=extra_params,
            output_dir=Path(_env_text(env, "BENCH_OUTPUT_DIR") or "results"),
            inter_trial_pause_s=_env_number(env, "BENCH_INTER_TRIAL_PAUSE_S", 3.0),
            max_run_duration_s=_env_number(env, "BENCH_MAX_RUN_DURATION_S", 8700),
            shutdown_grace_s=_env_number(env, "BENCH_SHUTDOWN_GRACE_S", 10),
            unset_env_vars=tuple(unset),
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
    """Builds token-targeted prompts with a unique per-request prefix."""

    def __init__(self, model_hint: str = "o200k_base") -> None:
        import tiktoken

        try:
            self._enc = tiktoken.get_encoding(model_hint)
        except ValueError:
            self._enc = tiktoken.get_encoding("cl100k_base")
        self._system_tokens = len(self._enc.encode(SYSTEM_MESSAGE))
        self._body_cache: dict[int, list[int]] = {}

    def build(self, target_input_tokens: int, variant: str) -> str:
        """Return user text with an early cache-busting request marker."""
        budget = max(16, target_input_tokens - self._system_tokens)
        body_tokens = self._body_cache.get(target_input_tokens)
        if body_tokens is None:
            filler_tokens = max(1, len(self._enc.encode(_FILLER)))
            repeats = math.ceil(budget / filler_tokens) + 1
            body_tokens = self._enc.encode(_PROMPT_PREFIX + _FILLER * repeats)
            self._body_cache[target_input_tokens] = body_tokens

        marker = hashlib.sha256(variant.encode()).hexdigest()
        marker_tokens = self._enc.encode(f"Request marker {marker}. ")
        return self._enc.decode((marker_tokens + body_tokens)[:budget])


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

    def _kwargs(
        self,
        deployment: str,
        wl: Workload,
        stream: bool,
        prompt_variant: str,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            **self._cfg.extra_generation_params,
            "model": deployment,
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": self._prompts.build(
                        wl.input_tokens,
                        prompt_variant,
                    ),
                },
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
        kwargs = self._kwargs(
            deployment_name,
            wl,
            stream,
            f"{run_id}:{scenario.scenario_id}:{trial}:{seq}",
        )

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
            error_message = _safe_error_message(exc)

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
        "queue_delay_s": {
            "p50": percentile(queue, 50),
            "p95": percentile(queue, 95),
            "max": max(queue) if queue else None,
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "prompt_per_s": (
                round(prompt_tokens / elapsed_s, 2) if elapsed_s else None
            ),
            "completion_per_s": (
                round(completion_tokens / elapsed_s, 2)
                if elapsed_s
                else None
            ),
            "total_per_s": (
                round((prompt_tokens + completion_tokens) / elapsed_s, 2)
                if elapsed_s
                else None
            ),
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
    if (
        not isinstance(cfg.endpoint, str)
        or not cfg.endpoint.strip()
        or cfg.endpoint.lstrip().startswith("<")
    ):
        raise ValueError("endpoint is unresolved")
    endpoint_error = _endpoint_validation_error(cfg.endpoint)
    if endpoint_error is not None:
        raise ValueError(endpoint_error)

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


async def warm_up(
    executor: Executor,
    cfg: Config,
    scenario: Scenario,
    label: str,
    name: str,
    run_id: str,
) -> None:
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
                            scenario_hash = (
                                int(
                                    hashlib.sha256(
                                        scenario.scenario_id.encode()
                                    ).hexdigest(),
                                    16,
                                )
                                % 1000
                            )
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
        try:
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
        finally:
            try:
                await client.close()
            finally:
                try:
                    await http_client.aclose()
                finally:
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
        ),
        encoding="utf-8",
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
    content = completed.stdout.rstrip() + "\n"
    path = out_dir / "pip-packages.txt"
    path.write_text(content, encoding="utf-8")
    return {
        "file": path.name,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def write_manifest(
    cfg: Config,
    labels: list[str],
    run_id: str,
    out_dir: Path,
    config_source: str,
    dependency_snapshot: dict[str, str],
) -> None:
    """Record everything the experiment holds constant."""
    runner_path = Path(__file__).resolve()
    effective_config = _redact_sensitive_values(
        {
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
            "timeouts": {
                "connect_s": cfg.connect_timeout_s,
                "read_s": cfg.read_timeout_s,
            },
            "max_in_flight": cfg.max_in_flight,
            "prompt_cache_strategy": (
                "unique request marker before repeated prompt content"
            ),
            "workloads": [
                {
                    "name": workload.name,
                    "input_tokens": workload.input_tokens,
                    "max_output_tokens": workload.max_output_tokens,
                }
                for workload in cfg.workloads
            ],
        }
    )
    experiment = _redact_sensitive_values(cfg.experiment)
    manifest = {
        "run_id": run_id,
        "runner_version": RUNNER_VERSION,
        "runner_sha256": hashlib.sha256(runner_path.read_bytes()).hexdigest(),
        # Digest of the values actually used, since configuration now comes from
        # the environment rather than a file that could be hashed directly.
        "config_sha256": hashlib.sha256(
            json.dumps(
                {"experiment": experiment, "config": effective_config},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest(),
        "source": source_revision(runner_path.parent),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": experiment,
        "config_source": config_source,
        "config": effective_config,
        "system_message": SYSTEM_MESSAGE,
        "client": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": Path(sys.executable).name,
        },
        "auth": "DefaultAzureCredential (Entra ID); no API keys used",
        "retry_policy": {
            "attempts_per_request": 1,
            "sdk_max_retries": 0,
        },
        "dependency_snapshot": dependency_snapshot,
    }
    import azure.identity
    import openai

    manifest["client"]["openai"] = openai.__version__
    manifest["client"]["azure_identity"] = azure.identity.__version__

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )


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


def readiness_errors(
    cfg: Config,
    labels: Sequence[str],
    *,
    require_reference_pair: bool = False,
) -> list[str]:
    errors: list[str] = []
    if require_reference_pair:
        missing_labels = [
            label
            for label in DEFAULT_DEPLOYMENT_LABELS
            if label not in cfg.deployments
        ]
        if missing_labels:
            expected_variables = ", ".join(
                f"{DEPLOYMENT_ENV_PREFIX}{_label_suffix(label)}"
                for label in missing_labels
            )
            errors.append(
                "full comparison is missing deployment label(s): "
                f"{', '.join(missing_labels)}; set {expected_variables}"
            )
    if (
        not isinstance(cfg.endpoint, str)
        or not cfg.endpoint.strip()
        or cfg.endpoint.lstrip().startswith("<")
    ):
        errors.append("endpoint is unresolved")
    else:
        endpoint_error = _endpoint_validation_error(cfg.endpoint)
        if endpoint_error is not None:
            errors.append(endpoint_error)
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

    if cfg.token_param not in ALLOWED_TOKEN_PARAMS:
        errors.append(
            "generation.token_param must be max_completion_tokens or max_tokens"
        )

    if not isinstance(cfg.extra_generation_params, dict):
        errors.append("generation.extra_params must be an object")
    else:
        controlled_request_keys = {
            "model",
            "messages",
            "stream",
            "stream_options",
            cfg.token_param,
        } | PROMPT_CACHE_CONTROL_KEYS
        overridden_keys = controlled_request_keys.intersection(
            cfg.extra_generation_params
        )
        if overridden_keys:
            errors.append(
                "generation.extra_params cannot override runner-controlled keys: "
                + ", ".join(sorted(overridden_keys))
            )
        sensitive_paths = _sensitive_key_paths(
            cfg.extra_generation_params,
            "generation.extra_params",
        )
        if sensitive_paths:
            errors.append(
                "generation.extra_params must not contain credential fields: "
                + ", ".join(sensitive_paths)
            )

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

    deployment_summary = ", ".join(
        f"{label} -> {cfg.deployments[label]}" for label in labels
    )
    print(f"deployments      : {deployment_summary}")
    print(f"workloads        : {', '.join(w.name for w in cfg.workloads)}")
    print(f"scenarios        : {len(scenarios)}")
    print(f"trials           : {cfg.trials}")
    print(f"total runs       : {runs}")
    print(
        f"trial duration   : {cfg.trial_duration_s:g}s "
        f"(+{cfg.inter_trial_pause_s:g}s pause)"
    )
    print(
        f"estimated wall   : {est_s / 60:.1f} min "
        f"({est_s / 3600:.2f} h), excluding warm-up"
    )
    print(
        f"wall-time limit  : {cfg.max_run_duration_s / 60:.1f} min, "
        "including warm-up and drain"
    )
    print(f"shutdown grace   : {cfg.shutdown_grace_s:g}s before the hard limit")
    nominal_slack_minutes = (
        cfg.max_run_duration_s - cfg.shutdown_grace_s - est_s
    ) / 60
    print(f"nominal slack    : {nominal_slack_minutes:.1f} min")
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
    ap = argparse.ArgumentParser(
        description="Global Standard vs. provisioned throughput benchmark"
    )
    config_source_group = ap.add_mutually_exclusive_group()
    config_source_group.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="dotenv file to load instead of .env",
    )
    config_source_group.add_argument(
        "--no-env-file",
        action="store_true",
        help="use process environment variables without loading a dotenv file",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the matrix and exit; contacts nothing",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=None,
        help="restrict to a deployment label (repeatable)",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="write artifacts here instead of results/<run-id>",
    )
    args = ap.parse_args()

    from dotenv import dotenv_values

    env_file = args.env_file or Path(".env")
    file_env: dict[str, str] = {}
    using_env_file = False
    if args.no_env_file:
        config_source = "environment"
    elif env_file.is_file():
        using_env_file = True
        try:
            parsed_env = dotenv_values(env_file, interpolate=False)
        except (OSError, UnicodeError) as exc:
            print(f"failed to load dotenv file {env_file}: {exc}", file=sys.stderr)
            return 2
        file_env = {
            name: value
            for name, value in parsed_env.items()
            if value is not None
        }
        shadowed = sorted(
            name
            for name, value in file_env.items()
            if value and name in os.environ and os.environ[name] != value
        )
        if shadowed:
            print(
                f"warning: process environment overrides {env_file.name}: "
                + ", ".join(shadowed),
                file=sys.stderr,
            )
        config_source = env_file.name
    elif env_file.exists():
        print(f"dotenv path is not a file: {env_file}", file=sys.stderr)
        return 2
    elif args.env_file is not None:
        print(f"dotenv file not found: {env_file}", file=sys.stderr)
        return 2
    else:
        config_source = "environment"

    if using_env_file:
        effective_env = {
            name: os.environ.get(name, value)
            for name, value in file_env.items()
        }
    else:
        effective_env = dict(os.environ)
    try:
        cfg = Config.from_env(effective_env)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    labels = list(args.only) if args.only else list(cfg.deployments)
    unknown = [l for l in labels if l not in cfg.deployments]
    if unknown:
        print(f"unknown deployment label(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(cfg.deployments)}", file=sys.stderr)
        return 2

    print_matrix(cfg, labels)

    errors = readiness_errors(
        cfg,
        labels,
        require_reference_pair=args.only is None,
    )

    def report(headline: str) -> None:
        print(f"\n{headline}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        if cfg.unset_env_vars:
            print("\nunset environment variables:", file=sys.stderr)
            for name in cfg.unset_env_vars:
                print(f"  - {name}", file=sys.stderr)
            print("see .env.example for the full list", file=sys.stderr)

    if args.dry_run:
        print("\ndry run: no network calls made, no resources touched")
        if errors:
            report("dry-run readiness failed:")
            return 2
        return 0

    if errors:
        report("refusing to run:")
        return 2

    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    out_dir = args.output_dir or (cfg.output_dir / run_id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dependency_snapshot = write_dependency_snapshot(out_dir)
        write_manifest(
            cfg,
            labels,
            run_id,
            out_dir,
            config_source,
            dependency_snapshot,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"failed to initialize benchmark output: {exc}", file=sys.stderr)
        return 2
    print(f"\nrun_id: {run_id}\noutput: {out_dir}\n")

    return run_with_process_watchdog(cfg, labels, run_id, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
