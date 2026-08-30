# PayGo vs. PTU Benchmarks on Microsoft Foundry

This project compares a Global Standard (pay-as-you-go) deployment with a
Provisioned Throughput (PTU) deployment on the same Microsoft Foundry resource.
It measures latency, throughput, throttling, and reliability under equivalent
load.

> **Capacity under test:** The reference configuration compares a 35-PTU
> GPT-5.6 Luna Global Provisioned deployment with a GPT-5.6 Luna Global Standard
> deployment configured for 1,000,000 TPM (about 95% of the PTU side's nominal
> capacity). This nominal match uses
> [Luna's documented rate of 30,000 input TPM per PTU](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing);
> actual capacity consumption also depends on the input/output workload mix.

## What it compares

| Category | Global Standard / PayGo | Provisioned Throughput |
|---|---|---|
| Billing | Input and output token usage | Provisioned capacity for the deployment lifetime |
| Capacity | Shared, usage-based capacity | Reserved, pre-allocated capacity |
| Best fit | Variable or bursty workloads | Predictable, sustained workloads |
| Latency | Can vary with shared demand | Typically steadier within provisioned capacity |
| Scaling | Subject to quota and service limits | Capacity must be sized in advance |
| Throttling | Depends on quota, rate limits, and shared capacity | Depends on provisioned capacity and workload shape |

Standard deployments do not provide a latency SLA. Provisioned deployments
have model- and configuration-specific latency targets; these are separate from
the Azure service availability SLA. See
[Provisioned throughput for Foundry Models](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput).

## Requirements

- Python 3.11 or newer.
- Access to the Microsoft Foundry resource under test.
- `Cognitive Services OpenAI User` or equivalent data-plane permission.
- Azure CLI, managed identity, workload identity, or another
  `DefaultAzureCredential` source.
- Network access to the Foundry endpoint.
- Matching Global Standard and PTU deployments that use the same model and
  model version.

> **Cost warning:** A PTU deployment incurs hourly charges while it exists.
> Starting or stopping this runner does not change that billing.

## Methodology

The default matrix uses three workloads:

| Workload | Target input tokens | Maximum output tokens |
|---|---:|---:|
| Short chat | 200 | 100 |
| RAG-style request | 1,000 | 300 |
| Long generation | 500 | 1,000 |

Input values are deterministic prompt-construction targets. Use the recorded
prompt-token counts in the results as the authoritative API measurements.

Each workload runs through:

- Closed-loop concurrency tests.
- Fixed offered-load tests at configured requests per minute.
- Streaming tests for time to first token.
- Three trials with alternating deployment order and deterministic scenario
  shuffling.

Every request is attempted once. SDK retries are disabled so throttling and
other failures remain visible.

The reference target of 357 RPM is calibrated to the RAG-style workload. With
Luna's 6:1 output-to-input normalization ratio, each request represents up to
2,800 normalized tokens, or approximately 999,600 normalized TPM at 357 RPM.
Using the same request rates for all three workloads intentionally tests how
different input/output shapes behave against the same deployments.

| Category | Recorded metrics |
|---|---|
| Latency | End-to-end p50, p90, p95, p99, and maximum |
| Streaming | Time to first token, completion time, and output-token cadence |
| Throughput | Successful requests per second, achieved RPM, and tokens per second |
| Reliability | Successes, HTTP 429s, timeouts, HTTP errors, exceptions, and invalid responses |
| Load pressure | Queue delay, peak in-flight requests, backlog, and unfinished requests |
| Usage | Prompt, completion, and total tokens |

Results with fewer than 100 successful samples include a warning because p95
and p99 values may not be statistically reliable.

## Setup

macOS, Linux, or a GitHub Codespace:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```



Edit `.env` and provide the resource-specific values:

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource root, such as `https://your-resource.openai.azure.com/` |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID` | Resource provenance recorded in the run manifest |
| `AZURE_RESOURCE_GROUP`, `AZURE_FOUNDRY_RESOURCE`, `AZURE_FOUNDRY_PROJECT` | Resource identity |
| `AZURE_DEPLOYMENT_GLOBAL_STANDARD`, `AZURE_DEPLOYMENT_PROVISIONED` | Deployment names |
| `BENCH_SKU_<LABEL>_NAME`, `BENCH_SKU_<LABEL>_CAPACITY` | Actual SKU names and capacities recorded as metadata |
| `BENCH_MODEL_NAME`, `BENCH_MODEL_VERSION`, `BENCH_REGION` | Deployed model and region |
| `BENCH_CLIENT_LOCATION` | Runner location, needed to interpret client latency |

Keep `AZURE_OPENAI_API_VERSION_VERIFIED=false` until the v1 endpoint has been
confirmed for the resource, then set it to `true`. The runner accepts only
HTTPS resource-root endpoints on the public Azure OpenAI domain.

Deployment variables are discovered by prefix. For example,
`AZURE_DEPLOYMENT_GLOBAL_STANDARD` creates the `global-standard` label used by
`--only global-standard`. Each additional deployment also needs matching
`BENCH_SKU_<LABEL>_NAME` and `BENCH_SKU_<LABEL>_CAPACITY` values.

For Global Standard, capacity is expressed in thousands of TPM, so `1000`
means 1,000,000 TPM. For Global Provisioned, capacity is expressed in PTUs, so
`35` means 35 PTUs. These values document the deployments; the runner does not
create, resize, or verify Azure capacity.

The `BENCH_*` values define the experiment. Comma-separated variables define
load levels; `BENCH_WORKLOADS` and `BENCH_GENERATION_EXTRA_PARAMS` contain JSON.
Real environment variables override values from `.env`. Use `--env-file` to
load a different dotenv file, or `--no-env-file` to use only the process
environment. An explicitly named dotenv file must exist.

Authenticate before a live run:

```bash
az login
az account show
```

## Run

Validate the complete configuration without network or Azure operations:

```bash
python app.py --dry-run
```

This validates local configuration and runtime estimates only. It does not test
authentication, network access, or whether the named deployments exist.

The following optional troubleshooting commands send model requests. Global
Standard requests incur token usage, and the PTU deployment continues incurring
hourly charges while it exists.

Each command runs the complete matrix against one deployment and has a nominal
duration of about 63.6 minutes. They are not required before the combined run.

```bash
python app.py --only global-standard
python app.py --only provisioned
```

Run the full comparison:

```bash
python app.py
```

The default full comparison contains 144 measured scenario executions and has
a nominal duration of 127.2 minutes, plus warm-up and request drain. Its hard
wall-clock limit is 145 minutes. Review the current estimate from `--dry-run`
before starting.

The runner warms both deployments before measurement, alternates deployment
order between trials, checkpoints aggregates after every measured scenario,
and enforces a hard wall-clock limit.

## Output

Each live run creates a unique directory under `results/` unless
`--output-dir` is provided:

| File | Contents |
|---|---|
| `manifest.json` | Effective experiment settings, source revision, and client metadata |
| `pip-packages.txt` | Installed package names and versions |
| `requests.jsonl` | One sanitized record per completed request |
| `aggregates.json` | Per-scenario metrics and partial checkpoints |
| `stopped.json` | Stop reason when a deadline or interruption ends a run |

The runner does not collect Azure Monitor metrics. Export those separately
using the UTC start and end times in the aggregates.

## Interpret the results

- Compare the same workload, load level, and trial across deployments.
- Treat p95 and p99 values with fewer than 100 successful samples as
  directional only.
- Use queue delay and client backlog to distinguish runner-side saturation from
  service latency.
- Review HTTP 429 and timeout rates alongside throughput; lower latency from a
  heavily throttled run is not a better result.
- Correlate client results with Azure Monitor metrics over each trial's UTC
  window before drawing capacity conclusions.

## Security and sharing

- The runner uses Microsoft Entra ID through `DefaultAzureCredential`; it does
  not accept or write Azure OpenAI API keys.
- Do not store client secrets, access tokens, passwords, or API keys in dotenv
  files. Prefer `az login`, managed identity, or workload identity. For
  automation, inject secrets from a secret store into the process environment.
- `.env` variants and `results/` are ignored by Git. Only the blank
  `.env.example` template is tracked.
- Hand off the project through Git when possible. If sharing an archive, exclude
  `.env`, `.venv`, `results/`, and `__pycache__/`.
- The endpoint allowlist prevents Entra bearer tokens from being sent to
  arbitrary hosts. Recorded error messages and sensitive generation parameter
  fields are redacted.
- Prompts and model responses are not written to the result artifacts.
- Result artifacts contain non-secret but potentially sensitive resource
  metadata, including subscription and tenant IDs, deployment names, and the
  endpoint. Review artifacts before sharing them.
- The dependency snapshot records package names and versions, not installation
  URLs that could contain repository credentials.

## Tests

The tests build an isolated configuration and do not read `.env`:

```bash
python -m unittest -v test_app.py
```

## Cleanup

After the benchmark:

1. Confirm that result and aggregate files were written.
2. Export the corresponding Azure Monitor metrics.
3. Record the PTU deployment lifetime for cost analysis.
4. Delete the PTU deployment if it is no longer needed.
5. Confirm in Microsoft Foundry that deletion completed.

PTU billing continues until the deployment itself is deleted.
