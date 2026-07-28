# Using the Microsoft Foundry Skill to Deploy a GlobalStandard Model

This guide walks through deploying a pay-as-you-go (GlobalStandard) model
deployment using the `microsoft-foundry` skill in GitHub Copilot CLI, following
the benchmark runbook in `03-ptuVSpaygo.md`.

## Prerequisites

- Azure Skills plugin installed in Copilot CLI (`/plugin install azure@azure-skills`)
- Authenticated to Azure (`az login`)
- An existing Foundry resource and project
- `Cognitive Services Contributor` role on the target resource

## Step 1: Verify the Foundry Skill Is Available

Run `/skills` in Copilot CLI and confirm `microsoft-foundry` appears in the
list. If missing, update the plugin:

```
/plugin update azure@azure-skills
```

## Step 2: Discover Your Foundry Context

Ask the skill to list your projects without making any changes:

```
List my Microsoft Foundry projects and show the parent Foundry resource,
resource group, subscription, and region for each. Do not create or modify
anything.
```

Record the project resource ID, Foundry resource name, resource group,
subscription, and region in your benchmark manifest.

## Step 3: Find Models That Support Both Deployment Types

Use the skill to discover which models and versions support both GlobalStandard
and provisioned throughput:

```
For project <project-resource-id>, discover model versions that support both
GlobalStandard and a provisioned throughput deployment type. Show the exact
SKU names, supported regions, and model versions. Do not deploy anything.
```

From the output, select a model and version that supports both SKUs. Record the
exact model name, version, and available SKU names.

## Step 4: Check Quota and Capacity

Confirm your subscription has unallocated quota for the GlobalStandard
deployment:

```
For subscription <subscription-id>, region <region>, model <model>, and version
<model-version>, show available Global Standard and provisioned quota. Separate
subscription quota from platform capacity. Do not request quota or deploy.
```

Verify that:
- Global Standard quota has headroom for your planned capacity
- The target region has platform capacity
- Provisioned quota exists for the later PTU deployment

## Step 5: Deploy the GlobalStandard Model (Customize Path)

Use the customize workflow so you can review every setting before the skill
creates anything:

```
Deploy model <model-name> version <model-version> as a GlobalStandard
deployment named "<model-name>-global-standard" with capacity <N> on Foundry
resource <foundry-resource> in resource group <resource-group>. Use the
customize workflow. Set version-upgrade policy to NoAutoUpgrade. Show all
settings for review before deploying.

List my Microsoft Foundry projects and show the parent Foundry resource,
resource group, subscription, and region for each. Do not create or modify
anything.
``` 

The skill uses the `models/deploy-model` sub-skill with the **customize** route,
which gives full control over:
- Model version (pinned for benchmark reproducibility)
- SKU name and capacity
- Content-filter policy (must match the PTU side later)
- Version-upgrade policy (`NoAutoUpgrade` during the benchmark window)

**Do not confirm** until you have verified every setting matches your manifest.

## Step 6: Verify the Deployment

After the skill completes the deployment, confirm it succeeded:

```
Show the provisioning state, model, version, SKU, and capacity of deployment
"<model-name>-global-standard" on Foundry resource <foundry-resource>. Also
show the endpoint URL.
```

Record the endpoint in `bench.config.json` and `03-cli-variables.md`.

## Step 7: Validate the Benchmark Runner

At this point, return to section 8 of `03-ptuVSpaygo.md` and validate the
benchmark runner against the GlobalStandard deployment. Do **not** create the
provisioned deployment until the runner completes a full validation pass.

## What Happens Under the Hood

The `microsoft-foundry` skill routes your request through these sub-skills:

| Step | Sub-skill | Purpose |
|------|-----------|---------|
| Discovery | `models/deploy-model` → `capacity` | Find SKUs and availability |
| Quota check | `quota` | Verify subscription headroom |
| Deployment | `models/deploy-model` → `customize` | Full-control deployment |
| Troubleshooting | `troubleshoot` | If deployment fails |

The skill calls the Azure MCP `foundry` tool for each operation and pauses for
confirmation before any mutating action.

## Key Reminders

- **GlobalStandard bills per token** — it costs nothing while idle, making it
  safe for runner debugging.
- **Use customize, not preset** — the benchmark requires pinned versions and
  matching content-filter policies across both deployments.
- **Do not deploy the PTU side yet** — hourly billing starts the moment that
  deployment is created, regardless of traffic.
- **Record everything** — the skill output feeds directly into the experiment
  manifest (section 3 of the runbook).
