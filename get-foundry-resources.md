

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



```bash
# `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID` 
az account show --query "{SubscriptionId:id, TenantId:tenantId}" -o table

```