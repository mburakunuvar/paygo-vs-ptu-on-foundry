# Benchmark Status Summary — Global Standard vs. Provisioned Throughput

Runbook: [03-ptuVSpaygo.md](03-ptuVSpaygo.md)
Date: 2026-07-28 (last updated)

## Status at a glance

Everything so far is **read-only or local**. No Azure resources exist and nothing is
billing — a live check of `ptu-benchmarks-resource` on 2026-07-28 returns zero
deployments.

The runbook runs in [phase order, not numeric order](03-ptuVSpaygo.md): the runner
(section 8) is built before anything is deployed (sections 6-7).

| Phase | Runbook step | Status |
|---|---|---|
| 1. Plan | 1-3 | Done, except the contract placeholders below |
| 2. Discover | 4-5 | Done — model, SKUs, quota, capacity, and prices confirmed live |
| 3. Build | 8 | Runner written; never run against a live endpoint |
| 4. Baseline deploy | 6, 7.1 | Not started |
| 5. Validate | 8 | Not started |
| 6-10 | 7.2-7.3, 9-14 | Not started |

**Open gate:** step 7.1 creates the first billable deployment (tokens only); step 7.2
starts hourly PTU billing. Approval for the Azure bill is still required.

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

| Model | Provisioned SKU | Verdict |
|---|---|---|
| `gpt-5.6-luna` | none | Rejected — no provisioned side to compare |
| `gpt-5.6-sol` | `GlobalProvisionedManaged` | **Selected** |
| `gpt-5.6-terra` | `DataZoneProvisionedManaged` only | Rejected — routing mismatch |

`gpt-5.6-sol` wins because its baseline and provisioned SKUs are **both globally
routed**, so the runbook's routing-asymmetry caveat does not apply. `terra` is half
the token price with double the PTU efficiency, but would have forced either a routing
confound or a deviation from the `GlobalStandard` baseline.

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
| API version | `2026-05-01-preview` — assumed, not yet verified |
| Endpoint / deployment names | Placeholders — fill after steps 7.1-7.3 |
| Runner version / seed | `1.0.0` / `20260727` |

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
shape.** PTU only becomes competitive under a reservation. `terra` produces the same
~3x ratio despite half the token price and double the PTU efficiency — the two effects
cancel — so the finding is **structural, not model-specific**.

This does not invalidate the benchmark. Latency, TTFT, throttling, and saturation
shape are the real deliverables, and PTU should win clearly on p95/p99 and 429 rate.
But the cost conclusion must be framed against reservation pricing, not hourly.

## Planned run: 3 hours

Capped at three billed hours, ~$185-225.

| Window | Activity |
|---|---|
| 0-10 min | Create both deployments, verify parity, smoke test |
| 10-155 min | Measurement passes |
| 155-170 min | Export results, aggregates, and manifest |
| 170-180 min | Delete both deployments |

The full matrix fits, so no scenarios are dropped:

| Pass | Levels | Runs (x2 deployments, x3 trials) |
|---|---|---|
| Concurrency sweep | 1, 2, 4, 8, 16, 32 | 36 |
| Offered-load sweep | 9, 18, 27 RPM | 18 |
| TTFT pass (streaming) | 4, 18 RPM | 12 |
| **Total** | 11 scenarios | **66 runs** |

`--dry-run` estimates **102 minutes** at `trial_duration_s: 90` plus a 3 s pause,
inside the 145-minute window — roughly 40 minutes of slack. Warm-up runs once per
deployment per workload.

Trial length was raised from 60 s to 90 s because longer trials cost **no extra PTU**
(PTU bills on deployment lifetime, not trial length) and buy sample count. 120 s was
rejected: it needs 135 of the 145 minutes, leaving no room for a stall or restart
before the run eats into the export slot while PTU bills. If a run still overruns,
drop `trials` from 3 to 2 rather than extending the window, and record the deviation
in the manifest.

**Accepted limitation:** a 90-second trial at concurrency 1 yields only ~45 requests,
so p99 is not credible there. Report p50 and p90 at low concurrency and reserve
p95/p99 claims for the high-concurrency passes. The runner flags this itself — any
scenario under 100 successful samples gets a `sample_warning` in its aggregate.

## Runner state

| Artifact | State |
|---|---|
| `app.py` | 873 lines, runner version `1.0.0` |
| `bench.config.json` | Full 11-scenario matrix; endpoint and deployment names are placeholders |
| `.venv/` | `openai 2.48.0`, `azure-identity 1.25.3`, `httpx 0.28.1`, `tiktoken 0.13.0` |
| `results/` | Does not exist — no run has produced output |

Covers the section 8 checklist: Entra ID auth only, excluded warm-up, closed- and
open-loop sweeps, a streaming TTFT pass, alternating deployment order, seeded case
order, per-request results, percentile aggregates, error and throttle classification,
and an immutable manifest. Deployment names are configuration, not code.

**Retries are disabled by design.** Neither the SDK nor the runner retries, so a 429
is recorded as a result rather than hidden behind a backoff. The unused retry loop and
its per-request fields were removed on 2026-07-28.

### Outstanding

1. **Never run against a live endpoint.** `--dry-run` passes — config parses and the
   11 scenarios build — but it makes no network calls, so authentication, token
   accounting, and TTFT detection are all unproven.
2. **`results/pip-freeze.txt` not captured.** The runbook requires resolved SDK
   versions in the manifest before measurement.
3. **API version unverified.** `2026-05-01-preview` is assumed, not discovered.

## Next step

**Step 6 (CLI variables), then step 7.1** — create the GlobalStandard deployment only.
Step 6 is non-mutating; 7.1 bills per token, so it is cheap and is the endpoint the
runner must be validated against.

Step 7.2 stays gated: it starts $45/hour billing at creation regardless of traffic —
$135 across the approved window, plus $45 for every hour cleanup is late.
