# Benchmark Global Standard (Pay-As-You-Go) vs. Provisioned Throughput

This guide describes how to prepare and run a repeatable benchmark between two
Microsoft Foundry model deployments:

- A pay-as-you-go `GlobalStandard` deployment
- A provisioned throughput deployment discovered from the model catalog

Both deployments use the same model and model version on the same Foundry
resource. The benchmark compares latency, throughput, throttling, utilization,
and cost under the same offered load.

> [!IMPORTANT]
> This document is a runbook, not an automated deployment. The commands that
> create or delete resources are examples for a future manual run. Review the
> selected subscription, region, model, capacity, and current pricing before
> running any command marked **MANUAL MUTATING STEP**.

## At a glance

Sections are numbered by topic, but they are **not executed in numeric order**.
Only the provisioned deployment bills by the hour, so the benchmark runner is
built and debugged before that capacity is created.

| Phase | Sections | Outcome | Cost |
|---|---|---|---|
| 1. Plan | 1-3 | Experiment contract and manifest | None |
| 2. Discover | 4-5 | Confirmed model, SKUs, quota, capacity, price | None |
| 3. Build | 8 | Benchmark runner, complete and reviewable | None |
| 4. Baseline deploy | 6, 7.1 | Pay-as-you-go deployment | Tokens only |
| 5. Validate | 8 | Runner proven against a live endpoint | Tokens only |
| 6. Provision | 7.2, 7.3 | Provisioned capacity, parity verified | **Hourly billing starts** |
| 7. Measure | 9-10 | Client and Azure metrics | Hourly |
| 8. Release | 14 (provisioned only) | Provisioned capacity deleted | **Hourly billing stops** |
| 9. Analyze | 11-13 | Latency, throughput, and cost comparison | None |
| 10. Clean up | 14 (baseline) | Remaining deployment removed | None |

### Why this order

`GlobalStandard` bills per token and costs nothing while idle. Provisioned
throughput bills for every hour the deployment exists, whether or not a single
request is sent. Creating both at the same time means every runner defect —
authentication, token accounting, TTFT event detection, failure classification, output
schema — is discovered while provisioned capacity sits idle and billing.

Building the runner first and validating it against the pay-as-you-go deployment
moves that debugging into a phase costing cents rather than tens of dollars per
hour. The provisioned deployment is created only once the runner is known to work
end to end, and is deleted as soon as measurement finishes. Analysis runs against
exported files, so it needs neither deployment.

The result is only meaningful if both deployments differ in exactly one way:
deployment type and its capacity. Every other variable is held constant.

## 1. Understand the scope

Model deployments are managed on the parent Foundry resource. A Foundry project
organizes the application, agents, evaluations, and other project assets that
consume those deployments.

```text
Azure subscription
└── Resource group
    └── Foundry resource
        ├── Project used by the demo
        ├── <model>-global-standard
        └── <model>-provisioned
```

The baseline in this guide is `GlobalStandard`. Global Standard can route
requests across Azure regions, while the provisioned SKU available to the
selected model might be region-bound. If the catalog offers a matching global
provisioned SKU, prefer it. Otherwise, record the routing difference as an
experimental limitation rather than attributing every result to reserved
capacity.

## 2. Prerequisites

Before planning a deployment, confirm the following:

- An existing Foundry resource and project
- Permission to inspect the resource, model catalog, quota, and metrics
- `Cognitive Services Contributor` or equivalent permission to create deployments
- `Cognitive Services OpenAI User` or equivalent data-plane permission for the benchmark identity
- Azure CLI and the Microsoft Foundry Azure skill available in the development environment
- Network access from the benchmark host to the Foundry endpoint
- Separate quota for Global Standard and provisioned throughput in the target subscription and region
- An isolated Python virtual environment for the asynchronous benchmark runner
- Approval for PTU charges or reservations before provisioned capacity is created

### Python environment

Create a project-local virtual environment. Do not install runner dependencies
into the system or global interpreter: a benchmark result is only reproducible if
the SDK versions that produced it are pinned and isolated, and the runbook
requires recording the API and SDK versions as fixed variables.

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

Invoke the runner through the environment's own interpreter (`./.venv/bin/python`)
rather than activating the shell, so that every command is unambiguous about which
interpreter ran it.

Before creating a client or sending a request, each non-dry runner invocation writes
the resolved environment to `results/<run-id>/pip-freeze.txt` and records its SHA-256
digest in the manifest. Failure to capture that snapshot aborts the run.

Exclude `.venv/` from version control.

Do not use API keys in source files or benchmark output. Prefer Microsoft Entra
ID authentication, such as `DefaultAzureCredential`, for the future runner.

## 3. Define the experiment contract

Create a benchmark manifest before deploying anything. Fill in values only
after live discovery.

| Setting | Value to record |
|---|---|
| Benchmark date and UTC window | `<date and time>` |
| Subscription and tenant | `<subscription-id>` / `<tenant-id>` |
| Resource group | `<resource-group>` |
| Foundry resource and project | `<foundry-resource>` / `<project>` |
| Foundry resource region | `<region>` |
| Benchmark client location | `<host and Azure region, if applicable>` |
| Model and exact version | `<model>` / `<model-version>` |
| Data-plane API surface | Azure OpenAI v1, verified live |
| Baseline deployment type | `GlobalStandard` |
| Provisioned deployment type | `<live-discovered provisioned SKU>` |
| Global Standard capacity | `<live-discovered capacity>` |
| Provisioned capacity | `<official calculator result>` |
| Content-filter policy | `<same policy for both>` |
| Version-upgrade policy | `NoAutoUpgrade` during the benchmark window |
| Generation settings | `<temperature, top_p, max output tokens, seed>` |
| Benchmark runner version | `<commit or release>` |
| Random seed | `<seed>` |
| Maximum runner wall time | `<approved measurement window>` |

Hold the following variables constant:

- Model name and exact model version
- Content-filter policy
- System message, prompts, and expected token ranges
- Generation parameters and maximum output tokens
- API and SDK versions
- Client machine, network path, and connection configuration
- Warm-up policy, request order, trial duration, and one-attempt policy

Do not enable dynamic quota, spillover, or priority processing for only one side
of the experiment. These features can change request routing or capacity
behavior and make the result harder to interpret.

## 4. Discover the Foundry context with the Azure skill

Use the Microsoft Foundry Azure skill for live discovery. Availability changes
by subscription, region, model, version, and date, so do not start with a
hardcoded SKU or PTU count.

Suggested prompts:

```text
List my Microsoft Foundry projects and show the parent Foundry resource,
resource group, subscription, and region for each. Do not create or modify
anything.
```

```text
For project <project-resource-id>, discover model versions that support both
GlobalStandard and a provisioned throughput deployment type. Show the exact
SKU names, supported regions, and model versions. Do not deploy anything.
```

```text
For subscription <subscription-id>, region <region>, model <model>, and version
<model-version>, show available Global Standard and provisioned quota. Separate
subscription quota from platform capacity. Do not request quota or deploy.
```

```text
Prepare a custom deployment configuration for two deployments using the same
model version and content-filter policy: one GlobalStandard and one supported
provisioned SKU. Stop before deployment and show every setting for review.
```

Record the skill output in the manifest. Confirm all of these before moving on:

1. The exact model version supports both deployment types.
2. The target region has platform capacity.
3. The subscription has unallocated quota for both deployment types.
4. The selected Foundry project maps to the expected parent resource.
5. The provisioned SKU is regional or global, and that difference is recorded.

## 5. Size the two deployments

### Provisioned capacity

Use an official capacity calculation method:

1. Open Microsoft Foundry.
2. Select **Operate** > **Quota**.
3. Open the **Provisioned throughput unit** tab.
4. Select **Capacity calculator**.
5. Enter the exact model/version, expected requests per minute, input tokens per request, output tokens per request, and latency target.
6. Save the inputs and recommended PTU capacity in the manifest.

The management-plane `calculateModelCapacity` REST API is another official
option when a repeatable calculation is needed. Use the current API schema from
the official REST reference rather than copying a stale request body.

### Global Standard capacity

Calculate the expected input and output tokens per minute for the same workload.
Select Global Standard capacity with documented headroom, subject to live quota.
Do not treat TPM and PTU as directly interchangeable units. Fairness comes from
offering the same workload to both deployments and measuring each saturation
curve.

### Cost checkpoint

Before any provisioned deployment is created:

1. Retrieve current prices for the model, deployment types, region, and currency.
2. Determine whether the test uses hourly provisioned capacity or a reservation.
3. Estimate the minimum test cost and the cost if cleanup is delayed.
4. Obtain explicit approval from the person responsible for the Azure bill.

## 6. Prepare CLI variables

The following block only defines shell variables. It does not contact Azure.

```bash
SUBSCRIPTION_ID="<subscription-id>"
RG_NAME="<resource-group>"
FOUNDRY_NAME="<foundry-resource>"
LOCATION="<region>"

MODEL_NAME="<model-name>"
MODEL_VERSION="<exact-model-version>"
MODEL_FORMAT="OpenAI"

GLOBAL_DEPLOYMENT_NAME="${MODEL_NAME}-global-standard"
GLOBAL_SKU_NAME="GlobalStandard"
GLOBAL_SKU_CAPACITY="<capacity-from-live-quota-planning>"

PTU_DEPLOYMENT_NAME="${MODEL_NAME}-provisioned"
PTU_SKU_NAME="<live-discovered-provisioned-sku>"
PTU_SKU_CAPACITY="<capacity-from-official-calculator>"
```

For a future authenticated session, inspect the active account before any
resource operation:

```bash
az account show \
  --query "{Subscription:name, SubscriptionId:id, TenantId:tenantId}" \
  -o table
```

Changing the active CLI subscription affects subsequent commands but does not
create a resource:

```bash
az account set --subscription "$SUBSCRIPTION_ID"
```

Check remaining quota in the target region. Confirm both the Global Standard and
provisioned entries for the selected model have headroom:

```bash
az cognitiveservices usage list \
  --location "$LOCATION" \
  -o table
```

Inspect existing deployments before choosing names:

```bash
az cognitiveservices account deployment list \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --query "[].{Name:name, Model:properties.model.name, Version:properties.model.version, Sku:sku.name, Capacity:sku.capacity}" \
  -o table
```

## 7. Create the deployments

> [!CAUTION]
> The commands in this section are **MANUAL MUTATING STEPS**. They create
> billable model deployments. Do not run them until live catalog, quota,
> capacity, target resource, and cost have been reviewed and approved.

> [!IMPORTANT]
> Do not run 7.1 and 7.2 in one sitting. Create the pay-as-you-go deployment in
> 7.1, then return to section 8 and validate the runner against it. Create the
> provisioned deployment in 7.2 only once the runner works end to end. See
> [Why this order](#why-this-order).

Both commands pin the same model and version. `az cognitiveservices account
deployment create` does not expose content-filter or version-upgrade options in
every CLI version, so confirm the available flags first:

```bash
az cognitiveservices account deployment create --help
```

If those settings are not available as flags, apply the same content-filter
policy and disable automatic version upgrades on both deployments through the
Foundry portal or the management API, then verify parity in step 7.3 before
benchmarking.

### 7.1 Create Global Standard

Create this deployment **before** the provisioned one. It bills per token, so it
costs nothing while idle and can host runner validation cheaply.

**MANUAL MUTATING STEP:**

```bash
az cognitiveservices account deployment create \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$GLOBAL_DEPLOYMENT_NAME" \
  --model-name "$MODEL_NAME" \
  --model-version "$MODEL_VERSION" \
  --model-format "$MODEL_FORMAT" \
  --sku-name "$GLOBAL_SKU_NAME" \
  --sku-capacity "$GLOBAL_SKU_CAPACITY"
```

**Record deployment outputs** — confirm provisioning state and capture the
endpoint URL for the runner config:

```bash
az cognitiveservices account deployment show \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$GLOBAL_DEPLOYMENT_NAME" \
  --query "{State:properties.provisioningState, Model:properties.model.name, Version:properties.model.version, Sku:sku.name, Capacity:sku.capacity}" \
  -o table

az cognitiveservices account show \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --query 'properties.endpoints."OpenAI Language Model Instance API"' \
  -o tsv
```

Update `bench.config.json` with the deployment name and endpoint before
proceeding to runner validation.

### 7.2 Create provisioned throughput

Do not reach this point until the runner has completed a full validation pass
against the deployment from 7.1. Hourly billing begins the moment this deployment
is created, not when the first request is sent.

Pause and reconfirm the provisioned SKU, PTU capacity, pricing, and target
resource. Quota confirms subscription headroom but not platform capacity —
capacity is only proven when creation returns `Succeeded`, so verify state before
starting the measurement clock.

**MANUAL MUTATING STEP:**

```bash
az cognitiveservices account deployment create \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$PTU_DEPLOYMENT_NAME" \
  --model-name "$MODEL_NAME" \
  --model-version "$MODEL_VERSION" \
  --model-format "$MODEL_FORMAT" \
  --sku-name "$PTU_SKU_NAME" \
  --sku-capacity "$PTU_SKU_CAPACITY"
```

**Record deployment outputs** — confirm provisioning succeeded and note the
timestamp (billing starts now):

```bash
az cognitiveservices account deployment show \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$PTU_DEPLOYMENT_NAME" \
  --query "{State:properties.provisioningState, Model:properties.model.name, Version:properties.model.version, Sku:sku.name, Capacity:sku.capacity}" \
  -o table
```

Update `bench.config.json` with the provisioned deployment name. The measurement
clock is now running.

### 7.3 Verify deployment parity

These commands are read-only:

```bash
az cognitiveservices account deployment show \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$GLOBAL_DEPLOYMENT_NAME" \
  -o json

az cognitiveservices account deployment show \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$PTU_DEPLOYMENT_NAME" \
  -o json
```

Check that both deployments have succeeded and match on model name, model
version, content-filter policy, and upgrade policy. They should differ only in
deployment type and planned capacity.

Retrieve the parent Foundry endpoint without exposing credentials:

```bash
az cognitiveservices account show \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --query 'properties.endpoints."OpenAI Language Model Instance API"' \
  -o tsv
```

Run one identical smoke request against each deployment before starting the
benchmark. Exclude smoke requests and warm-up requests from measured results.

## 8. Design the benchmark runner

> [!NOTE]
> Build this section **before** section 7. The runner is written and reviewed
> while nothing is deployed, then validated against the pay-as-you-go deployment
> from 7.1, and only then pointed at provisioned capacity.

The runner uses `AsyncOpenAI` with Entra ID authentication and the Azure OpenAI
v1 base URL, `<openai-endpoint>/openai/v1/`. The v1 data plane does not take an
`api-version` query parameter. Keep benchmark implementation separate from
provisioning.

Install and run it from the project-local virtual environment described in
[Python environment](#python-environment). Pin dependencies in `requirements.txt`
and capture the resolved versions alongside the results, because SDK version is
one of the variables the experiment holds constant.

Deployment names must be configuration, not code. Moving from the validation
phase to the measurement phase should require changing a config value only, so
that no code path is exercised for the first time while provisioned capacity is
billing.

The configuration must also record the model/version, deployment SKUs and
capacities, shared content-filter and upgrade policies, benchmark-client location,
and whether the API surface was verified live. The runner must refuse a live run
while any required value is missing or a placeholder. It must also refuse when the
nominal matrix already consumes the configured wall-clock limit, because warm-up
and request drain still need time inside that limit.

`--dry-run` performs these readiness checks without creating credentials or making
network calls. It exits nonzero when a selected deployment or required experiment
value is unresolved.

Readiness validation must reject empty workload or pass lists, duplicate/nonpositive
load levels, nonpositive token targets or timing values, malformed deployment-SKU
metadata, and non-boolean API verification. When both sides are selected, deployment
names and SKU names must be distinct so the benchmark cannot compare a deployment
or deployment type against itself.

Create at least three stable workload classes:

| Workload | Purpose | Example shape |
|---|---|---|
| Short chat | Interactive latency | Short input, short output |
| RAG or summarization | Typical application request | Medium/large input, medium output |
| Long generation | Decode throughput | Fixed input, large capped output |

Record actual input and output token counts. Do not compare scenarios with
materially different token distributions as if they were equivalent.

The runner should implement:

- One excluded warm-up phase per deployment and workload
- Connection reuse with explicit connection and read timeouts
- Alternating deployment order between trials
- Randomized case order using a recorded seed
- Closed-loop concurrency sweeps, such as 1, 2, 4, 8, 16, and 32 workers
- Open-loop offered-load sweeps below, near, and above planned capacity
- A bounded fixed-worker queue for open-loop arrivals; do not create one asyncio
  task per scheduled request
- A separate streaming pass for time to first token
- At least three measured trials per scenario
- Request-level JSONL or CSV output plus an immutable run manifest
- Exactly one request attempt, with SDK retries disabled so 429 responses remain visible
- A hard wall-clock deadline that includes warm-up, trials, pauses, and request drain
- Atomic aggregate checkpoints after every completed trial, plus a partial active
  trial summary during graceful deadline cancellation

An unsuccessful warm-up is a readiness failure, not a measured sample. Abort before
the matrix on authentication, API-surface, deployment, or other warm-up errors.
Every measured trial must record explicit UTC start and end timestamps. Preserve
completed raw output and aggregates if the hard deadline cancels an in-progress run.

Closed-loop tests answer how the deployment behaves with a fixed number of
active clients. Open-loop tests answer whether it can sustain a fixed arrival
rate without growing a queue. Use both; a closed-loop client can hide overload
by slowing its own request generation.

## 9. Benchmark matrix and metrics

Run the same matrix against each deployment:

| Pass | Load | Mode | Primary purpose |
|---|---|---|---|
| Warm-up | Low | Non-streaming | Excluded connection/model warm-up |
| Baseline | Concurrency 1 | Non-streaming | Unloaded end-to-end latency |
| Concurrency sweep | Increasing workers | Non-streaming | Scaling and saturation |
| Offered-load sweep | Below/near/above target | Non-streaming | Sustainable throughput |
| TTFT pass | Low and near target | Streaming | First-token and token cadence |

The checked-in matrix has 24 scenarios: three concurrency levels, three
offered-load levels, and two streaming levels across three workloads. With two
deployments and three trials, that is 144 measured trial runs. At 50 seconds plus
a 3-second pause per run, nominal measurement time is 127.2 minutes, leaving 17.6
minutes inside the 145-minute limit for warm-up and request drain.

Collect these client-side metrics:

- End-to-end latency p50, p90, p95, and p99
- Streaming time to first content-bearing token (TTFT)
- Mean output-token interval derived from completion-token usage, and stream completion latency
- Successful requests per second
- Input tokens, output tokens, and total tokens per second
- Output tokens per second for successful completions
- HTTP 429 rate, other error rate, and timeout rate; retries are fixed at zero
- Offered arrival rate, achieved arrival rate, client queue delay, and backlog

Do not mix streaming and non-streaming latency samples in one distribution.
Measure TTFT at the first content-bearing stream event, not at response headers
or an empty role event.

Streaming events are chunks, not guaranteed one-token events. Do not label time
between chunks as inter-token latency. Derive mean output-token cadence from the
reported completion-token count and disclose that it is a per-request average,
not a distribution of individual token arrival intervals.

## 10. Collect Azure-side metrics

Record the UTC start and end of every trial. Use the same windows in Azure
Monitor for both deployments and collect the metrics currently exposed for the
selected model and deployment types, including where available:

- Request count
- Processed prompt and generated completion tokens
- Service latency
- Throttled requests and HTTP errors
- Provisioned-managed utilization

Azure Monitor aggregation can lag behind the client run. Export metrics after
the aggregation window has completed. Keep service-side metrics separate from
client-side measurements because client latency includes network time, local
queuing, and response processing.

## 11. Compare cost and utilization

Use prices retrieved on the benchmark date. Do not store default prices in the
runner or this guide.

For Global Standard, calculate:

```text
standard cost
  = input tokens × current input-token rate
  + output tokens × current output-token rate
```

For provisioned throughput, calculate:

```text
allocated PTU cost
  = provisioned capacity × current hourly rate × elapsed billed hours
```

If a reservation is used, report its term and amortized cost separately. Then
compare:

- Cost of the measured benchmark window
- Cost per 1,000 successful requests
- Cost per million successfully processed tokens
- Observed PTU utilization
- Projected monthly cost at stated duty cycles
- Estimated utilization or traffic level where PTU becomes cost-competitive

Label every projection with the pricing date, region, currency, and assumptions.
Performance and cost conclusions should not be generalized beyond the measured
model, version, workload, region/routing mode, and load range.

## 12. Interpret the results

Use latency distributions and saturation curves, not averages alone.

Treat saturation as the point where one or more of these occurs:

- Throughput stops increasing with offered load
- p95 or p99 latency rises materially
- Client or service backlog grows
- HTTP 429, timeout, or error rates begin increasing
- Provisioned utilization remains near its supported ceiling

Before drawing a conclusion, verify:

1. The load generator itself was not CPU, network, socket, or connection-pool limited.
2. Token distributions were comparable between deployments.
3. The one-attempt policy remained active, so retries did not hide throttling.
4. Reversing deployment order did not reverse the result.
5. At least three trials show the same pattern.
6. Global versus regional routing is disclosed when the deployment types do not have matching routing scope.

This benchmark tests service behavior, not subjective response quality. Basic
response-validity checks are useful, but model quality should be evaluated in a
separate experiment.

## 13. Troubleshooting and stopping criteria

| Symptom | Check |
|---|---|
| Provisioned SKU is missing | Confirm exact model/version and region support through live catalog discovery |
| Deployment reports insufficient quota | Check the separate regional quota entry; do not substitute platform capacity for subscription quota |
| Deployment cannot be reached | Check public network access, private endpoints, DNS, firewall rules, and benchmark-host location |
| Many 429 responses | Preserve them as benchmark results; verify quota/capacity and confirm the one-attempt policy before changing the test |
| Latency rises on both deployments identically | Check the load generator, network, connection pool, and client queue |
| TTFT is zero or implausibly small | Ensure timing starts before the request and stops at the first content-bearing event |
| Azure Monitor totals differ | Confirm retries remained disabled; account for failed calls, UTC boundaries, dimensions, and aggregation delay |

Stop the benchmark if costs exceed the approved budget, the runner saturates,
the two deployments are not configuration-equivalent, or monitoring shows an
unexpected impact on another workload sharing the resource.

The runner's configured wall-clock limit is a mandatory stop, not an estimate. It
must cancel in-flight work when reached and return a nonzero exit status. This does
not delete provisioned capacity: immediately follow the cleanup procedure when a
deadline or warm-up failure stops the run.

## 14. Cleanup after the benchmark

Before cleanup, export the manifest, deployment metadata, raw request results,
aggregates, Azure Monitor data, and pricing inputs.

> [!CAUTION]
> The following commands are **MANUAL MUTATING STEPS**. They permanently delete
> deployments. Confirm the target resource and names before running them.

Delete the **provisioned deployment first**, as soon as measurement ends and
results are exported. It is the only deployment charging by the hour, and
analysis (sections 11-13) runs entirely against exported files. Leaving it in
place during analysis is the single most common way to overspend on this test.

Delete Global Standard only if it was created solely for this demo:

```bash
az cognitiveservices account deployment delete \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$GLOBAL_DEPLOYMENT_NAME"
```

Delete the provisioned deployment after confirming results have been exported:

```bash
az cognitiveservices account deployment delete \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --deployment-name "$PTU_DEPLOYMENT_NAME"
```

List deployments and quota again to verify capacity was released. Deleting a
deployment does not necessarily cancel a separate provisioned reservation or
commitment. Verify its billing status independently in Azure.

## References

### Official Microsoft documentation

- [Provisioned throughput for Foundry Models](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput)
- [Provisioned throughput onboarding](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-onboarding)
- [Foundry model deployment types](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types)
- [Azure OpenAI quota management](https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/quota)
- [Azure OpenAI quotas and limits](https://learn.microsoft.com/en-us/azure/ai-services/openai/quotas-limits)
- [Calculate model capacity REST API](https://learn.microsoft.com/en-us/rest/api/aiservices/accountmanagement/calculate-model-capacity/calculate-model-capacity)
- [Azure OpenAI pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/)
- [Azure Monitor supported metrics for Microsoft.CognitiveServices](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-metrics/microsoft-cognitiveservices-accounts-metrics)
- [Authenticate Azure-hosted Python applications](https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication-overview)
- [Azure skill for Microsoft Foundry](https://learn.microsoft.com/en-us/azure/developer/azure-skills/skills/microsoft-foundry)

### Community tools

These tools can help explore scenarios, but their output must be checked against
the official Foundry capacity calculator, live quota, model catalog, and current
Azure pricing before deployment:

- [PTU Calculator](https://www.ptucalc.com/)
- [Azure PTU Calculator user guide](https://github.com/ricmmartins/azureptucalc/blob/main/docs/USER_GUIDE.md)
- [Microsoft field PTU Advisor](https://github.com/msftse/ptu-advisor/)
