# PayGo vs. PTU Benchmark on Microsoft Foundry

## PTU vs Pay-As-You-Go Benchmark

This project benchmarks **Provisioned Throughput (PTU)** against **Global Standard (Pay-As-You-Go)** deployments on the same Microsoft Foundry resource to compare latency, throughput, throttling, and cost under identical load.

### Key Differences

| | Global Standard (PayGo) | Provisioned Throughput (PTU) |
|---|---|---|
| **Billing** | Per token consumed | Per hour (whether used or not) |
| **Routing** | Global (cross-region) | Regional (dedicated capacity) |
| **Best for** | Variable/bursty workloads | Predictable, high-throughput workloads |
| **Latency** | Shared capacity, variable | Reserved capacity, consistent |
| **Cost risk** | Scales with usage | Clock starts at deployment creation |

### Implementation Plan

The benchmark follows a phased approach designed to minimize PTU cost exposure:

1. **Plan** — Define the experiment contract and record all variables
2. **Discover** — Confirm model, SKUs, quota, and pricing via the Foundry skill
3. **Build** — Develop the async benchmark runner (`app.py`) with dry-run validation
4. **Baseline Deploy** — Create the Global Standard deployment (tokens-only cost)
5. **Validate** — Prove the runner works end-to-end against PayGo
6. **Provision** — Create the PTU deployment (**hourly billing starts**)
7. **Measure** — Run the benchmark matrix (3 workloads × concurrency/load sweeps × 3 trials)
8. **Release** — **Delete PTU immediately** after measurement to stop billing
9. **Analyze** — Compare latency distributions, throughput curves, and cost
10. **Clean up** — Remove remaining deployments

### Benchmark Design

- **Runner**: Async Python using `AsyncOpenAI` + Entra ID auth
- **Workloads**: Short chat, RAG/summarization, long generation
- **Load patterns**: Closed-loop concurrency sweeps + open-loop offered-load sweeps
- **Metrics**: E2E latency (p50–p99), TTFT, tokens/sec, 429 rate, utilization
- **Controls**: Same model/version, content filter, generation params, single-attempt policy
- **Safety**: Hard wall-clock deadline, warm-up validation, config-driven deployment switching

### Project Files

| File | Purpose |
|---|---|
| `03-ptuVSpaygo.md` | Full runbook with all procedures and commands |
| `bench.config.json` | Benchmark runner configuration |
| `app.py` | Benchmark runner implementation |
| `test_app.py` | Runner test suite |
| `results/` | Raw output, aggregates, and manifests |

### Quick Start

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python app.py --dry-run          # validate config
./.venv/bin/python app.py --side standard    # run against PayGo
./.venv/bin/python app.py --side provisioned # run against PTU
```

See [`03-ptuVSpaygo.md`](03-ptuVSpaygo.md) for the complete runbook.

---

## Test Results (2026-07-28)

**Model**: `gpt-5.6-sol` v2026-07-09 · **Region**: swedencentral · **Endpoint**: `ptu-benchmarks-resource.openai.azure.com`

Two benchmark runs completed against the same Foundry resource. Results below are averaged across runs.

| | Global Standard (30 capacity) | Provisioned (45 PTU, GlobalProvisionedManaged) |
|---|---|---|
| **Run IDs** | `20260728T175439Z` / `20260728T195619Z` | same |
| **Trials completed** | 24 scenarios × 2 runs | 35–39 scenarios × 2 runs (more trials completed) |
| **Auth** | Entra ID (`DefaultAzureCredential`) | same |
| **Retries** | Disabled (single attempt) | same |

### Unloaded Latency (Concurrency = 1)

| Workload | Metric | Global Standard | Provisioned | Δ |
|---|---|---|---|---|
| **short-chat** | p50 latency | 1.918 s | 1.492 s | **−22%** |
| | throughput | 156 tok/s | 204 tok/s | +31% |
| **rag** | p50 latency | 4.437 s | 3.343 s | **−25%** |
| | throughput | 283 tok/s | 377 tok/s | +33% |
| **long-gen** | p50 latency | 12.988 s | 9.789 s | **−25%** |
| | throughput | 104 tok/s | 142 tok/s | +37% |

### Time to First Token (Streaming, 9 RPM)

| Workload | Global Standard | Provisioned | Δ |
|---|---|---|---|
| **short-chat** | 0.838 s | 0.741 s | **−12%** |
| **rag** | 0.847 s | 0.769 s | **−9%** |
| **long-gen** | 0.830 s | 0.749 s | **−10%** |

### Offered Load at Target (18 RPM)

| Workload | Metric | Global Standard | Provisioned |
|---|---|---|---|
| **short-chat** | p50 latency | 2.090 s | 1.617 s |
| | success rate | 37/38 (97%) | 38/38 (100%) |
| | 429 rate | 0.0% | 0.0% |
| **rag** | p50 latency | 4.755 s | 3.404 s |
| | success rate | 28/28 (100%) | 45/45 (100%) |
| | 429 rate | 0.0% | 0.0% |
| **long-gen** | p50 latency | 13.913 s | 10.220 s |
| | success rate | 36/36 (100%) | 55/66 (83%) |
| | 429 rate | 0.0% | **18.3%** |

### Offered Load at Saturation (27 RPM)

| Workload | Metric | Global Standard | Provisioned |
|---|---|---|---|
| **short-chat** | p50 latency | 2.041 s | 1.510 s |
| | 429 rate | 0.0% | 1.0% |
| **rag** | p50 latency | 4.698 s | 3.418 s |
| | 429 rate | 0.0% | 0.0% |
| **long-gen** | p50 latency | 13.268 s | 10.169 s |
| | 429 rate | 0.0% | **30.0%** |

### Key Observations

1. **PTU delivers 22–25% lower latency** across all workloads at low concurrency
2. **TTFT is 9–12% faster** on provisioned throughput
3. **Throughput is 31–37% higher** on PTU at concurrency = 1
4. **Global Standard handles burst better** — zero 429s even at 27 RPM, while PTU shows throttling on `long-gen` at 18+ RPM (capacity-bound at 45 PTU)
5. **PTU saturates on long-generation workloads** — the 45-PTU allocation is undersized for sustained 18+ RPM of 1000-token outputs
6. Both runs show consistent patterns, confirming result reproducibility

> **Note**: These are interim results from trial 0–1 of a planned 3-trial matrix. Full analysis pending completion of all trials. Pricing comparison deferred to section 11 of the runbook.

Raw results: [`results/`](results/)

---

## Comprehensive Comparison Report (Interim, 2026-07-28)

This section consolidates the completed validation and benchmark evidence currently
available in the repository. It is the most complete PayGo vs PTU comparison we can
make right now, but it is still an interim report because the full 3-trial matrix has
not finished yet.

### Evidence Base

- Unit test suite: 30 tests passed, covering request construction, queue timing,
	cadence aggregation, warm-up ordering, deadline handling, and error classification.
- Benchmark artifacts: the latest run produced manifests, dependency snapshots, raw
	rows, and aggregates under `results/20260728T195619Z-4020a8f2/`.
- Coverage status: 121 of 144 planned aggregate scenarios are present in the latest
	artifact run, with trial 2 still incomplete.
- Comparison scope: Global Standard (PayGo) and Provisioned Throughput (PTU) were
	exercised against the same model, version, region, content filter, and generation
	policy.

### Bottom Line

PTU is consistently faster on latency and TTFT in the completed samples, while
Global Standard is more forgiving under bursty or saturated load and avoids hourly
idle cost. The strongest completed evidence says PTU is the better choice for
predictable, sustained traffic when low latency matters more than hourly billing,
but PayGo is the safer default for bursty workloads and for any case where you do
not want a deployment clock running.

### Completed Comparison

| Dimension | Global Standard (PayGo) | Provisioned Throughput (PTU) | Read on the completed tests |
|---|---|---|---|
| Unloaded latency | Higher across all three workloads | Lower by about 22-25% | PTU wins on steady-state latency |
| TTFT (streaming, 9 RPM) | 0.838 s / 0.847 s / 0.830 s | 0.741 s / 0.769 s / 0.749 s | PTU is about 9-12% faster |
| Throughput at concurrency = 1 | 156 / 283 / 104 tok/s | 204 / 377 / 142 tok/s | PTU is about 31-37% higher |
| Offered load at 18 RPM | Lower latency, no throttling | Lower latency, but long-gen shows throttling | PTU is faster, but capacity-sensitive |
| Offered load at 27 RPM | 0% 429 across workloads | 0-1% 429 on short-chat/rag, 30% on long-gen | PayGo handles burst better |

Workloads are ordered as short-chat, rag, long-gen.

### What the Completed Tests Say

1. PTU improves first-token and end-to-end latency when the system is not heavily
	 saturated.
2. PTU also improves token throughput in the unloaded case.
3. Global Standard is materially better at absorbing burst, especially on the long-
	 generation workload where PTU starts to throttle at target and saturation load.
4. The current 45-PTU allocation is adequate for the lighter workloads, but it is
	 undersized for sustained long-gen traffic at 18+ RPM.
5. The benchmark runner itself is behaving correctly: Entra ID auth, request
	 validation, streaming TTFT capture, and artifact emission are all working.

### Cost Interpretation

PTU costs $45/hour at 45 PTU for as long as the deployment exists, whether or not
requests are sent. Global Standard bills by token consumption and stays cheap while
idle. That means the performance delta favors PTU, but the cost delta favors PayGo
unless the workload is steady enough to justify the hourly capacity charge or a
reservation.

### Practical Conclusion

- Choose PTU if you need lower and more stable latency for a predictable workload,
	especially when the traffic shape is steady enough to justify reserved capacity.
- Choose Global Standard if your load is bursty, variable, or cost-sensitive when
	idle.
- For long-gen workloads, the current 45-PTU sizing should be treated as a lower
	bound rather than a final capacity recommendation.

### Limits of This Report

- The full planned 144-scenario matrix is not finished yet.
- p95 and p99 values are still weak in many scenarios because sample counts remain
	low in several completed passes.
- This report should be treated as the best available comparison from the completed
	tests, not the final benchmark conclusion.

Detailed raw data and manifests remain in [results/](results/), and the runbook is
still the source of truth for the full experiment design in
[03-ptuVSpaygo.md](03-ptuVSpaygo.md).