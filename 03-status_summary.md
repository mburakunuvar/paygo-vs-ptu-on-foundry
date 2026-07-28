# Benchmark Status Summary — Global Standard vs. Provisioned Throughput

## To-do

- [x] Steps 1-3 — Plan: experiment contract and manifest
- [x] Steps 4-5 — Discover: model, SKUs, quota, capacity, pricing confirmed
- [x] Step 8 — Build: benchmark runner written (`app.py`, `bench.config.json`)
- [x] Step 6 — CLI variables set (values in `03-cli-variables.md`)
- [x] Step 7.1 — Create GlobalStandard deployment (tokens-only billing)
- [x] Record 7.1 outputs (endpoint, state) into config and variables file
- [x] Validate runner client and executor against live endpoint (streaming and non-streaming)
- [ ] Capture automatic `results/<run-id>/pip-freeze.txt` on the first live run
- [x] Verify Azure OpenAI v1 data-plane API and endpoint
- [x] Step 7.2 — Create provisioned deployment (**$45/hr billing active**)
- [x] Record 7.2 outputs (state, timestamp) into config and variables file
- [x] Step 7.3 — Verify deployment parity and both request modes
- [x] Fix stale over-budget test and hanging async deadline test
- [x] Run full test suite (`python -m unittest test_app`) — 30 tests pass
- [x] Revise the matrix to fit the measurement window
- [x] Pass the fully resolved 24-scenario / 144-run dry-run
- [ ] Steps 9-10 — Run measurement passes and export Azure metrics
- [ ] Step 14 — Delete provisioned deployment (**stop billing**)
- [ ] Steps 11-13 — Analyze results
- [ ] Step 14 — Delete baseline deployment

---

Runbook: [03-ptuVSpaygo.md](03-ptuVSpaygo.md)
Date: 2026-07-28 17:52 UTC (last updated)

## Status at a glance

Both deployments are `Succeeded/Running`. The 45-PTU
`gpt-5.6-sol-provisioned` deployment was created on 2026-07-28 at 17:49 UTC;
**$45/hour billing is active until that deployment is deleted.** Azure OpenAI v1
non-streaming and streaming requests pass through the runner's real client and
executor on both deployments.

The post-deployment parity smoke returned `200` for both deployments in both modes:

| Deployment | Mode | Total latency | TTFT | Usage |
|---|---|---:|---:|---:|
| Global Standard | Non-streaming | 3.124 s | — | 74 in / 64 out |
| Provisioned | Non-streaming | 1.419 s | — | 74 in / 64 out |
| Global Standard | Streaming | 1.187 s | 0.704 s | 74 in / 64 out |
| Provisioned | Streaming | 1.336 s | 1.221 s | 74 in / 64 out |

Both streaming responses contained 61 content chunks. These excluded diagnostics
prove authentication, request, usage, and TTFT paths only; they are not benchmark
measurements.

| Phase | Runbook step | Status |
|---|---|---|
| 1. Plan | 1-3 | Done |
| 2. Discover | 4-5 | Done — model, SKUs, quota, capacity, and prices confirmed live |
| 3. Build | 8 | Runner complete; 30 local tests pass in about 1.5 seconds |
| 4. Baseline deploy | 6, 7.1 | **Done** — `gpt-5.6-sol-global-standard` deployed 2026-07-28T16:43:06Z |
| 5. Validate | 8 | Client/executor smoke passed on both deployments; artifact-producing run remains |
| 6. Provision | 7.2-7.3 | **Done** — 45 PTU created, parity verified |
| 7-10 | 9-14 | Measurement, metrics, analysis, and cleanup not started |

**Active cost:** PTU billing started at 2026-07-28T17:49:01Z. The current retail
meter is $1.00/PTU/hour, so 45 PTU costs $45/hour until deletion. The deployment
name and parity metadata are resolved, and the matrix fits its configured limit.

The GlobalStandard deployment bills per token only and costs nothing while idle.

## Environment and target

| Item | Value |
|---|---|
| Azure CLI / Python | 2.88.0 / 3.12.1 |
| Signed in as | `burak-admin@MngEnvMCAP371870.onmicrosoft.com` |
| Subscription | `burak-MS` (`9103cd46-543d-4b44-a957-f011acb997c6`) |
| Tenant | `6ee29205-81b5-4e4b-b235-5bd9d6fb6b04` |
| Foundry resource | `ptu-benchmarks-resource` / `rg-foundry-customer-demos` / swedencentral |

`ptu-benchmarks-resource` was chosen because it is empty and can host both deployment
types on one resource. The `Endpoint1-PTU` / `Endpoint2-PayGo` pair was rejected: it
splits the two deployment types across separate Foundry resources.

## Discovery

All three candidates are version `2026-07-09`.

`gpt-5.6-sol` was selected because its baseline and provisioned SKUs are **both
globally routed** (`GlobalStandard` and `GlobalProvisionedManaged`), so the
runbook's routing-asymmetry caveat does not apply.

Live quota (swedencentral) and SKU limits:

| Quota entry | Used | Limit | Min | Step |
|---|---|---|---|---|
| `OpenAI.GlobalStandard.gpt-5.6-sol` | 0 | 1000 | — | 10 |
| `OpenAI.GlobalProvisionedManaged` | 0 | 100 PTU | 15 | 5 |

`AIServices.GlobalProvisionedManaged` shows a limit of 0, but does not apply:
`gpt-5.6-sol` is model format `OpenAI` and draws from the `OpenAI.*` pool.

## Sizing: 45 PTU vs. 30 GS units

Sized with the official `calculateModelCapacity` API (`2026-05-01`), not an assumed
conversion ratio.

| RPM | Input | Output | PTU returned |
|---|---|---|---|
| 18 | 1000 | 300 | **45** |
| 19 | 1000 | 300 | 45 (ceiling) |
| 20 | 1000 | 300 | 50 |

The calculator exposes its weighting: input x1.0, output x6.0, giving **1 PTU = 1,200
weighted tokens/min**. At the chosen **18 RPM** that is `18 x 2800 = 50,400` weighted
tokens against a `45 x 1200 = 54,000` ceiling — **1.07x headroom**. 19 RPM would leave
1.015x, effectively none.

Matching GlobalStandard capacity: `18 x 1300 = 23,400` TPM -> **30 units** (30,000 TPM,
step 10), or 1.28x headroom.

45 PTU is within the 100 PTU quota and is a valid step value (15 + 5x6).

### Experiment contract

| Setting | Value |
|---|---|
| Model / version | `gpt-5.6-sol` / `2026-07-09` |
| Region | `swedencentral` |
| Baseline | `GlobalStandard`, capacity 30 (30,000 TPM) |
| Provisioned | `GlobalProvisionedManaged`, capacity 45 PTU |
| Workload | 18 RPM, 1000 in / 300 out |
| Data-plane API | Azure OpenAI v1 — verified live; no `api-version` query |
| Endpoint / deployment names | GS: `gpt-5.6-sol-global-standard`; PTU: `gpt-5.6-sol-provisioned`; endpoint: `https://ptu-benchmarks-resource.openai.azure.com/` |
| Client location | GitHub Codespaces dev container; host region is not exposed |
| Generation policy | `max_completion_tokens` with `reasoning_effort: none` |
| Runner version / seed | `1.2.0` / `20260727` |
| Maximum runner wall time | 145 minutes, including warm-up and request drain |

## Cost

Prices retrieved 2026-07-27, swedencentral, USD, from the Azure retail prices API.

| Meter (`gpt-5.6-sol`, GlobalStandard, Global routing) | Price |
|---|---|
| Input | $5.00 / 1M tokens |
| Output | $30.00 / 1M tokens |
| Cached input | $0.50 / 1M tokens |

| Provisioned Managed Global Unit | Price |
|---|---|
| Hourly (consumption) | **$1.00 / PTU / hour** |
| 1-month reservation | $260 / PTU / month |
| 1-year reservation | $2,652 / PTU / year |

**Decision: hourly, no reservation.** A reservation would cost $11,700/month for
45 PTU and cannot be cancelled to fit a short test.

PTU bills on **deployment lifetime, not usage** — 45 PTU costs $45.00/hour from
creation to deletion whether or not a request is sent.

| Scenario | PTU | GS tokens | Total |
|---|---|---|---|
| **3-hour benchmark (planned)** | $135 | ~$50-90 | **~$185-225** |
| Cleanup delayed 24h | $1,080 | — | ~$1,150 |
| Cleanup forgotten 1 month | $32,850 | — | **~$32,850** |

### Break-even

Pricing alone already answers the runbook's step 11 question:

- 45 PTU ceiling: ~19.3 RPM = 1,157 requests/hour
- The same requests on GlobalStandard: 1,157 x $0.014 = **$16.20/hour**

| Billing | Effective $/hr | vs. PayGo at 100% utilization |
|---|---|---|
| Hourly | $45.00 | **2.8x worse** |
| 1-month reservation | $16.03 | ~break-even |
| 1-year reservation | $13.62 | ~16% better |

**At full utilization, hourly PTU costs 2.8x more than pay-as-you-go for this workload
shape.** PTU only becomes competitive under a reservation. The finding is
**structural, not model-specific**.

This does not invalidate the benchmark. Latency, TTFT, throttling, and saturation
shape are the real deliverables, and PTU should win clearly on p95/p99 and 429 rate.
But the cost conclusion must be framed against reservation pricing, not hourly.

## Planned run: 3 hours

Capped at three billed hours, ~$185-225.

| Window | Activity |
|---|---|
| 0-10 min | Create PTU deployment, verify parity, smoke test |
| 10-155 min | Measurement passes |
| 155-170 min | Export results, aggregates, and manifest |
| 170-180 min | Delete both deployments |

The checked-in matrix fits within the 145-minute measurement window and includes
both a low and a target-rate streaming pass.

| Pass | Levels | Runs (x3 workloads, x2 deployments, x3 trials) |
|---|---|---|
| Concurrency sweep | 1, 8, 32 | 54 |
| Offered-load sweep | 9, 18, 27 RPM | 54 |
| TTFT pass (streaming) | 9, 18 RPM | 36 |
| **Total** | 24 scenarios | **144 runs** |

At `trial_duration_s: 50` plus a 3 s pause, nominal time is **127.2 minutes**, leaving
about 17.6 minutes of execution headroom for warm-up and request drain.

**Accepted limitation:** short or low-rate trials will not produce credible p99s.
Report p50 and p90 at low sample counts and reserve p95/p99 claims for passes with
enough observations. Any scenario under 100 successful samples receives a
`sample_warning` in its aggregate.

## Runner state

| Artifact | State |
|---|---|
| `app.py` | Runner version `1.2.0`; Azure OpenAI v1 client and bounded open-loop worker pool |
| `test_app.py` | 30 tests pass in about 1.5 seconds, including deadline and checkpoint coverage |
| `bench.config.json` | 24-scenario matrix; verified v1 endpoint and both deployment names filled |
| `.venv/` | `openai 2.48.0`, `azure-identity 1.25.3`, `aiohttp 3.14.3`, `httpx 0.28.1`, `tiktoken 0.13.0` |
| `results/` | Does not exist — no run has produced output |

Covers the section 8 checklist: Entra ID auth only, excluded fail-fast warm-up,
closed- and open-loop sweeps, a streaming TTFT pass, alternating deployment order,
seeded case order, per-request results, percentile aggregates, explicit UTC trial
windows, error/throttle classification, peak client backlog, and an immutable
manifest. Deployment names are configuration, not code.

Open-loop scheduling uses exactly `max_in_flight` long-lived workers and a compact
arrival queue rather than one asyncio task per request. Completed trial aggregates
are atomically checkpointed, and graceful deadline cancellation records the active
trial as partial.

Streaming cadence is reported as mean output-token interval using completion-token
usage and the first/last content-bearing events; stream chunks are not treated as
tokens. Open-loop arrival rate uses the scheduled arrival window, while completion
and token throughput use total elapsed time including request drain.

**Retries are disabled by design.** Neither the SDK nor the runner retries, so a 429
is recorded as a result rather than hidden behind a backoff. The runbook and manifest
now make this one-attempt policy explicit.

### Outstanding

1. **Artifact-producing validation remains.** Direct calls through `build_client`
   and `Executor` proved Entra authentication, token accounting, response validation,
   and TTFT detection against both deployments. A normal CLI run is still needed
   to exercise manifest and result-file creation together.
2. **Dependency snapshot not yet produced.** Every normal non-dry invocation creates
   `results/<run-id>/pip-freeze.txt` and records its digest before client creation;
   no such invocation has occurred yet.
3. **Client region is unavailable.** The Codespaces host exposes no authoritative
   region value. This is disclosed in the manifest rather than guessed.

## Next step

Start the artifact-producing two-deployment measurement. Inspect the manifest,
raw rows, and aggregate checkpoints before exporting Azure metrics. Delete
`gpt-5.6-sol-provisioned` immediately after measurement and metric export; every
additional hour costs $45.
