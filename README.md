# PayGo vs. PTU Benchmarks on Microsoft Foundry

This project compares a Global Standard (pay-as-you-go) deployment with a
Provisioned Throughput (PTU) deployment on the same Microsoft Foundry resource.
It measures latency, throughput, throttling, and reliability under equivalent
load.

> **Capacity for Benchmark Test 1:** The reference configuration compares a 35-PTU
> GPT-5.6 Luna Global Provisioned deployment with a GPT-5.6 Luna Global Standard
> deployment configured for 1,000,000 TPM (about 95% of the PTU side's nominal
> capacity). This nominal match uses
> [Luna's documented rate of 30,000 input TPM per PTU](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing);
> actual capacity consumption also depends on the input/output workload mix.

> **Capacity for Benchmark Test 2:** The reference configuration compares a 35-PTU
> GPT-5.6 Terra Global Provisioned deployment with a GPT-5.6 Terra Global Standard
> deployment configured for 100,000 TPM (about 95% of the PTU side's nominal
> capacity). This nominal match uses
> [Terra's documented rate of 3,000 input TPM per PTU](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing);
> actual capacity consumption also depends on the input/output workload mix.

These are two independent experiments. Test 1 compares Luna PayGo with Luna
PTU, and Test 2 compares Terra PayGo with Terra PTU. Do not place all four
deployments in one run: model identity and load settings apply to the entire
process. Compare deployment types within the same model; a Luna-versus-Terra
comparison also includes model-performance differences.

> [Capacity risk](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput-billing)
> Unused quota doesn't guarantee that capacity is available when you want to
> scale a PTU deployment back up. Provisioned capacity is finite and changes
> dynamically, so scaling down can leave insufficient capacity later.

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

The runner places a unique marker at the beginning of every request prompt.
The marker is derived from the run, scenario, trial, and request sequence, so
corresponding PayGo and PTU requests receive the same content while different
requests do not reuse an automatically cached prompt prefix. This keeps prompt
caching from reducing the capacity pressure under test.

Both models use a 6:1 output-to-input normalization ratio. The RAG-style
workload therefore represents up to 2,800 normalized tokens per request:
`1,000 + (6 x 300)`. The profile-specific load matrices are:

| Profile | Below target | Target | Above target | Target normalized TPM |
|---|---:|---:|---:|---:|
| Luna | 180 RPM | 357 RPM | 530 RPM | 999,600 |
| Terra | 18 RPM | 35.7 RPM | 53 RPM | 99,960 |

The Luna target is a nominal match for 1,000,000 TPM. The Terra target uses
`100,000 / 2,800 = 35.7 RPM`, rounded to one decimal place, and remains below
the PTU side's nominal 105,000 TPM. Using each profile's rates for all three
workloads intentionally tests how different input/output shapes behave against
the same pair of deployments.

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
cp .env.luna.example .env.luna
cp .env.terra.example .env.terra
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.luna.example .env.luna
Copy-Item .env.terra.example .env.terra
```

Create and edit the profile for each model you plan to test. Always pass its
name with `--env-file`; running `python app.py` without that option looks for a
legacy `.env` file. The profiles are independent and must not be merged.

Provide these resource-specific values in each local profile:

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource root, such as `https://your-resource.openai.azure.com/` |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID` | Resource provenance recorded in the run manifest |
| `AZURE_RESOURCE_GROUP`, `AZURE_FOUNDRY_RESOURCE`, `AZURE_FOUNDRY_PROJECT` | Resource identity |
| `AZURE_DEPLOYMENT_GLOBAL_STANDARD`, `AZURE_DEPLOYMENT_PROVISIONED` | Matching deployment names for the profile's model |
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

For Global Standard, capacity is expressed in thousands of TPM. For Global
Provisioned, capacity is expressed in PTUs:

| Profile | Global Standard value | Global Standard capacity | Global Provisioned value |
|---|---:|---:|---:|
| Luna | `1000` | 1,000,000 TPM | 35 PTUs |
| Terra | `100` | 100,000 TPM | 35 PTUs |

These values document the deployments; the runner does not create, resize, or
verify Azure capacity.

The `BENCH_*` values define the experiment. Comma-separated variables define
load levels; `BENCH_WORKLOADS` and `BENCH_GENERATION_EXTRA_PARAMS` contain JSON.
Real environment variables override values from a profile file. Use
`--env-file` to select the Luna or Terra profile, or `--no-env-file` to use only
the process environment. With `--env-file`, only variables declared by that
profile participate in benchmark configuration; ambient variables cannot add
undeclared deployments. The runner warns when process variables shadow
nonempty file values. Profile files are parsed as configuration without being
exported into the process environment; credential variables for
`DefaultAzureCredential` must come from the actual process environment or a
developer login. Dotenv interpolation is disabled, and an explicitly named
dotenv file must exist.

`BENCH_GENERATION_EXTRA_PARAMS` cannot set model, message, streaming, token, or
prompt-cache control fields because those are owned by the runner.

To discover existing resources and generate each profile interactively, use
the matching tracked template. Run the command twice and select the appropriate
model pair each time:

```bash
./get-foundry-resources.sh --template .env.luna.example --output .env.luna
./get-foundry-resources.sh --template .env.terra.example --output .env.terra
```

The discovery helper requires Bash and an interactive terminal. On Windows,
run it from Git Bash or WSL, or fill the profile values manually in PowerShell.

Authenticate before a live run:

```bash
az login
az account show
```

## Run

Validate both configurations without network or Azure operations:

```bash
python app.py --env-file .env.luna --dry-run
python app.py --env-file .env.terra --dry-run
```

This validates local configuration and runtime estimates only. It does not test
authentication, network access, or whether the named deployments exist.

The following optional troubleshooting commands send model requests. Global
Standard requests incur token usage, and the PTU deployment continues incurring
hourly charges while it exists.

These commands run a profile's complete matrix against one deployment. They
are not required before the combined comparison.

```bash
python app.py --env-file .env.luna --only global-standard
python app.py --env-file .env.luna --only provisioned
python app.py --env-file .env.terra --only global-standard
python app.py --env-file .env.terra --only provisioned
```

Run both full comparisons separately:

```bash
python app.py --env-file .env.luna
python app.py --env-file .env.terra
```

Each full comparison contains 144 measured scenario executions. The Luna
profile has a nominal duration of 127.2 minutes and a 145-minute hard limit.
The Terra profile uses 180-second trials so its target-rate open-loop scenarios
schedule about 107 requests per trial; it has a nominal duration of 439.2
minutes and a 460-minute hard limit. Below-target Terra scenarios schedule
about 54 requests per trial, so their p95 and p99 values remain directional.
Both estimates exclude warm-up and request drain. Running both profiles takes
about 566.4 nominal minutes in total. Review each current estimate with
`--dry-run` before starting.

The runner warms both deployments before measurement, alternates deployment
order between trials, checkpoints aggregates after every measured scenario,
and enforces a hard wall-clock limit.

## Output

Each live run creates a unique directory under `results/luna/` or
`results/terra/`, according to its profile, unless `--output-dir` is provided:

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
- Compare PayGo and PTU only within the same model profile. Do not attribute
  Luna-versus-Terra differences solely to deployment type.
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
- Local `.env` variants and `results/` are ignored by Git. Only the blank base
  template and the two non-secret profile templates are tracked.
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

After each benchmark:

> **Warning:** PTU billing continues until the deployment itself is deleted.
> Remove it promptly unless it is still needed.

1. Confirm that result and aggregate files were written.
2. Export the corresponding Azure Monitor metrics.
3. Record the PTU deployment lifetime for cost analysis.
4. Delete the PTU deployment if it is no longer needed.
5. Confirm in Microsoft Foundry that deletion completed.

Repeat this cleanup for both model profiles.
