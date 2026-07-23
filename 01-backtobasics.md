

# Getting Started with Microsoft Foundry

## Foundry Resource and Project

- **Foundry resource** is the top-level Azure resource where you manage things like networking, security, model deployments, identity, billing, and monitoring. Multiple projects can live underneath the same resource

- **Foundry Project** is a child scope used to organize development work such as agents, evaluations, datasets/files, and other project assets. It can also have its own RBAC assignments while reusing capabilities from the parent resource.

```text
Azure Subscription
└── Resource Group
    └── Foundry Resource: company-ai
        ├── Project: customer-support
        │   ├── Agents
        │   ├── Evaluations
        │   └── Files / datasets
        │
        ├── Project: sales-assistant
        │   ├── Agents
        │   └── Evaluations
        │
        └── Shared governance / configuration
            ├── Networking
            ├── Security
            ├── Model deployments
            └── Connections
```


| | Foundry Resource | Foundry Project |
|---|---|---|
| Azure type | `Microsoft.CognitiveServices/accounts` | Child resource under account |
| Main purpose | Shared infrastructure and governance | Team/app workload isolation |
| Networking | Defined at parent scope | Inherits/uses parent settings |
| Model deployments | Managed centrally | Consumes available deployments |
| Artifacts (agents, evals, files) | Parent boundary | Organized per project |
| RBAC | Yes | Can also be project-scoped |
| Typical count | Fewer | Often many |

## Create and Manage MS Foundry Resouces

### 0. Create Resource Group, Foundry Resource, and Project

Set values once:

```bash
LOCATION=eastus
RG_NAME=my-foundry-rg
FOUNDRY_NAME=my-foundry-resource
PROJECT_NAME=my-foundry-project
```

Create the resource group:

```bash
az group create \
  --name "$RG_NAME" \
  --location "$LOCATION"
```

Create the Azure AI Foundry resource (`kind: AIServices`):

```bash
az cognitiveservices account create \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --location "$LOCATION" \
  --kind AIServices \
  --sku S0 \
  --allow-project-management true \
  --yes
```

Create a project under that Foundry resource:

```bash
az cognitiveservices account project create \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  --project-name "$PROJECT_NAME" \
  --location "$LOCATION"
```

Quick check:

```bash
az cognitiveservices account project list \
  --name "$FOUNDRY_NAME" \
  --resource-group "$RG_NAME" \
  -o table
```

### 1. List Azure Resource Groups

```bash
az group list -o table
```

Only names:

```bash
az group list --query "[].name" -o tsv
```

---

### 2. List Azure AI Foundry Resources

Azure AI Foundry resources are `Microsoft.CognitiveServices/accounts` with `kind: AIServices`.

```bash
az cognitiveservices account list \
  --query "[?kind=='AIServices'].{Name:name,ResourceGroup:resourceGroup,Location:location}" \
  -o table
```

List all Cognitive Services accounts (includes Foundry and other account types):

```bash
az cognitiveservices account list -o table
```

Reference:
https://learn.microsoft.com/en-us/cli/azure/cognitiveservices/account

---

### 3. List Azure AI Foundry Projects

Projects are child resources under a specific Foundry resource, so both parent name and resource group are required.

```bash
az cognitiveservices account project list \
  --name <foundry-resource-name> \
  --resource-group <resource-group-name> \
  -o table
```

Only project names:

```bash
az cognitiveservices account project list \
  --name <foundry-resource-name> \
  --resource-group <resource-group-name> \
  --query "[].name" \
  -o tsv
```

Reference:
https://learn.microsoft.com/en-us/cli/azure/cognitiveservices/account/project

---

## 4. Foundry Resource vs Foundry Project

Short rule:
Foundry Resource = platform boundary
Foundry Project = workload boundary

A Foundry resource is the top-level Azure boundary for governance and shared configuration (networking, security, identity, billing, monitoring, deployments). Multiple projects can exist under one resource.

A project is a scoped workspace for a specific team or application (agents, evaluations, files/datasets, and other app artifacts), with optional project-level access control.

Example hierarchy:

```text
Azure Subscription
└── Resource Group
    └── Foundry Resource: company-ai
        ├── Project: customer-support
        │   ├── Agents
        │   ├── Evaluations
        │   └── Files / datasets
        ├── Project: sales-assistant
        │   ├── Agents
        │   └── Evaluations
        └── Shared configuration
            ├── Networking
            ├── Security
            ├── Model deployments
            └── Connections
```




References:

- https://learn.microsoft.com/en-us/azure/foundry/concepts/architecture
- https://learn.microsoft.com/en-us/azure/foundry/how-to/create-projects
