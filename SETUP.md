# Setup Guide — Provisioning Azure Resources

This guide walks through manually provisioning the Azure resources needed to run this demo end-to-end. A future improvement will provide IaC (Bicep/Terraform) to automate this.

---

## Prerequisites

- Azure subscription with `Contributor` access
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed and logged in (`az login`)
- [GitHub CLI](https://cli.github.com/) installed and authenticated (`gh auth login`)
- Python 3.11+

---

## 1. Create Microsoft Foundry Resources

This demo uses two Foundry resources: one for **production** and one for **test** (used by the CI evaluate job on PRs). For a minimal setup, you can point both at the same resource.

### Option A — Single resource (simplest)
Use the same Foundry resource for both prod and test. Set both `AZURE_AI_PROJECT` and `AZURE_AI_PROJECT_TEST` secrets to the same endpoint.

### Option B — Separate prod and test resources (recommended)
Isolates the CI evaluate job from the production environment.

#### Create the resources

```bash
# Set your values
RESOURCE_GROUP="rg-my-agent-demo"
LOCATION="eastus"
PROD_NAME="my-agent-demo"
TEST_NAME="my-agent-demo-test"

# Create resource group
az group create --name $RESOURCE_GROUP --location $LOCATION

# Create Foundry resources (AIServices kind = New Foundry)
az cognitiveservices account create \
  --name $PROD_NAME \
  --resource-group $RESOURCE_GROUP \
  --kind AIServices \
  --sku S0 \
  --location $LOCATION \
  --yes

az cognitiveservices account create \
  --name $TEST_NAME \
  --resource-group $RESOURCE_GROUP \
  --kind AIServices \
  --sku S0 \
  --location $LOCATION \
  --yes
```

> **Important:** This demo requires the **New Foundry** portal (`AIServices` kind resource). Classic Azure OpenAI resources are not supported. The Evals API is only available on `services.ai.azure.com` endpoints.

---

## 2. Deploy Models

You need two model deployments on each resource:
- `gpt-4o-mini` — used as the agent model
- `gpt-4o` — used as the judge model for evaluations

```bash
# Deploy on PROD resource
az cognitiveservices account deployment create \
  --name $PROD_NAME \
  --resource-group $RESOURCE_GROUP \
  --deployment-name gpt-4o-mini \
  --model-name gpt-4o-mini \
  --model-version "2024-07-18" \
  --model-format OpenAI \
  --sku-name Standard \
  --sku-capacity 10

az cognitiveservices account deployment create \
  --name $PROD_NAME \
  --resource-group $RESOURCE_GROUP \
  --deployment-name gpt-4o \
  --model-name gpt-4o \
  --model-version "2024-11-20" \
  --model-format OpenAI \
  --sku-name Standard \
  --sku-capacity 10

# Repeat for TEST resource (replace $PROD_NAME with $TEST_NAME)
```

---

## 3. Get Your Project Endpoints

```bash
# Format: https://<resource-name>.services.ai.azure.com/api/projects/<project-name>
# The project name is typically the same as the resource name for new Foundry resources.

az cognitiveservices account show \
  --name $PROD_NAME \
  --resource-group $RESOURCE_GROUP \
  --query "properties.endpoint" -o tsv
```

Your project endpoint will be:
```
https://<resource-name>.services.ai.azure.com/api/projects/<resource-name>
```

---

## 4. Create a Service Principal for CI

```bash
SUBSCRIPTION_ID=$(az account show --query id -o tsv)

az ad sp create-for-rbac \
  --name "sp-agent-eval-gate" \
  --role "Contributor" \
  --scopes /subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP \
  --sdk-auth
```

Save the output. You'll need `clientId`, `clientSecret`, and `tenantId` for GitHub secrets.

### Grant the SP access to Foundry Agent Service

The service principal needs permission to create agents in Foundry:

```bash
SP_OBJECT_ID=$(az ad sp show --id <clientId-from-above> --query id -o tsv)

# Assign Azure AI User role on the Foundry resources
az role assignment create \
  --assignee $SP_OBJECT_ID \
  --role "Azure AI User" \
  --scope /subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP
```

---

## 5. Configure GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | Service principal client ID |
| `AZURE_CLIENT_SECRET` | Service principal client secret |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `AZURE_AI_PROJECT` | `https://<prod-resource>.services.ai.azure.com/api/projects/<prod-resource>` |
| `AZURE_AI_PROJECT_TEST` | `https://<test-resource>.services.ai.azure.com/api/projects/<test-resource>` |

Or set them via the GitHub CLI:

```bash
REPO="your-github-username/foundry-agent-eval-cicd"

gh secret set AZURE_CLIENT_ID --body "<value>" --repo $REPO
gh secret set AZURE_CLIENT_SECRET --body "<value>" --repo $REPO
gh secret set AZURE_TENANT_ID --body "<value>" --repo $REPO
gh secret set AZURE_SUBSCRIPTION_ID --body "<value>" --repo $REPO
gh secret set AZURE_AI_PROJECT --body "https://<prod-resource>.services.ai.azure.com/api/projects/<prod-resource>" --repo $REPO
gh secret set AZURE_AI_PROJECT_TEST --body "https://<test-resource>.services.ai.azure.com/api/projects/<test-resource>" --repo $REPO
```

---

## 6. Local Development Setup

```bash
# Clone and install
git clone https://github.com/your-username/foundry-agent-eval-cicd
cd foundry-agent-eval-cicd
pip install -r requirements.txt

# Copy and fill in your .env
cp .env.example .env
# Edit .env with your actual endpoints

# Authenticate
az login

# Create the agent in Foundry
python -m agent.agent_client

# Run the evaluation gate locally
python -m evals.run_eval_gate
```

---

## 7. Verify the Setup

After completing the above:

1. Open [Microsoft Foundry](https://ai.azure.com) → your project → **Agents** — you should see `azure-dev-assistant`
2. Open **Evaluations** — after running `run_eval_gate.py`, you should see a new eval run with results
3. Open your GitHub repo → push a test branch that touches `agent/agent.yaml` → watch the Actions tab


