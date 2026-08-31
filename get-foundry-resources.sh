#!/usr/bin/env bash
# Discover a matching Global Standard/PTU deployment pair and safely generate
# the benchmark's local .env file. This script only reads Azure resources.

set -Eeuo pipefail
IFS=$'\n\t'
TSV_DELIMITER=$'\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_FILE="$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")"
TEMPLATE_FILE="$SCRIPT_DIR/.env.example"
OUTPUT_FILE="$SCRIPT_DIR/.env"
TEMP_FILE=""

info() { printf '[info] %s\n' "$*" >&2; }
warn() { printf '[warning] %s\n' "$*" >&2; }
die() { printf '[error] %s\n' "$*" >&2; exit 1; }

cleanup() {
	if [[ -n "$TEMP_FILE" && -e "$TEMP_FILE" ]]; then
		rm -f -- "$TEMP_FILE"
	fi
}
trap cleanup EXIT
trap 'die "Setup stopped near line $LINENO."' ERR

usage() {
	cat <<'EOF'
Usage: ./get-foundry-resources.sh [--template PATH] [--output PATH]

Interactively discovers an existing Microsoft Foundry resource, project, and
matching Global Standard/PTU deployments, then generates a dotenv file. Azure
resources are never created or changed. Use a model-specific template to retain
that profile's calibrated load matrix and runtime settings.
EOF
}

while (($#)); do
	case "$1" in
		--template)
			(($# >= 2)) || die "--template requires a path."
			TEMPLATE_FILE="$2"
			shift 2
			;;
		--output)
			(($# >= 2)) || die "--output requires a path."
			OUTPUT_FILE="$2"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			die "Unknown argument: $1"
			;;
	esac
done

[[ -t 0 ]] || die "Run this script in an interactive terminal."
[[ -r "$TEMPLATE_FILE" ]] || die "Cannot read template: $TEMPLATE_FILE"
command -v az >/dev/null 2>&1 || die "Azure CLI is required. Install it, then run 'az login'."

if command -v python3 >/dev/null 2>&1 \
	&& python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
	PYTHON=python3
elif command -v python >/dev/null 2>&1 \
	&& python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
	PYTHON=python
else
	die "Python 3.11 or newer is required."
fi

if ! "$PYTHON" - "$OUTPUT_FILE" "$TEMPLATE_FILE" "$SCRIPT_FILE" <<'PY'
import os
import sys

output, template, script = map(os.path.realpath, sys.argv[1:])

def same_file(left: str, right: str) -> bool:
	if os.path.exists(left) and os.path.exists(right):
		return os.path.samefile(left, right)
	return os.path.normcase(left) == os.path.normcase(right)

raise SystemExit(same_file(output, template) or same_file(output, script))
PY
then
	die "The output path must not be the template or this script."
fi
[[ ! -d "$OUTPUT_FILE" ]] || die "The output path is a directory: $OUTPUT_FILE"

# Read dotenv values as text. The file is deliberately never sourced.
dotenv_get() {
	local path="$1" key="$2"
	[[ -r "$path" ]] || return 1
	"$PYTHON" - "$path" "$key" <<'PY'
import ast
import re
import sys

path, wanted = sys.argv[1:]
pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
found = None
with open(path, encoding="utf-8-sig") as stream:
	for raw_line in stream:
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue
		if line.startswith("export "):
			line = line[7:].lstrip()
		if "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		if not pattern.fullmatch(key) or key != wanted:
			continue
		value = value.strip()
		if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
			try:
				value = ast.literal_eval(value)
			except (SyntaxError, ValueError):
				pass
		found = str(value)
if found is not None:
	if any(ch in found for ch in "\r\n\0"):
		raise SystemExit(2)
	print(found, end="")
PY
}

existing() {
	dotenv_get "$OUTPUT_FILE" "$1" 2>/dev/null || true
}

template_default() {
	dotenv_get "$TEMPLATE_FILE" "$1" 2>/dev/null || true
}

prompt_value() {
	local prompt="$1" default="${2:-}" value
	while true; do
		if [[ -n "$default" ]]; then
			read -r -p "$prompt [$default]: " value
			value="${value:-$default}"
		else
			read -r -p "$prompt: " value
		fi
		if [[ -n "$value" && "$value" != *$'\n'* && "$value" != *$'\r'* ]]; then
			REPLY="$value"
			return
		fi
		warn "A non-empty single-line value is required."
	done
}

prompt_optional() {
	local prompt="$1" default="${2:-}" value
	if [[ -n "$default" ]]; then
		read -r -p "$prompt [$default] (Enter to keep): " value
		value="${value:-$default}"
	else
		read -r -p "$prompt (Enter to complete manually later): " value
	fi
	if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
		warn "Ignoring a value containing a line break."
		value=""
	fi
	REPLY="$value"
}

confirm() {
	local prompt="$1" answer
	read -r -p "$prompt [y/N]: " answer
	[[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

select_row() {
	local prompt="$1"
	shift
	local -a rows=("$@")
	local choice i
	((${#rows[@]} > 0)) || return 1
	printf '\n%s\n' "$prompt" >&2
	for i in "${!rows[@]}"; do
		printf '  %d) %s\n' "$((i + 1))" "${rows[$i]//$'\t'/  |  }" >&2
	done
	while true; do
		read -r -p "Select 1-${#rows[@]}: " choice
		if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#rows[@]})); then
			SELECTED_ROW="${rows[$((choice - 1))]}"
			return 0
		fi
		warn "Enter a number from 1 to ${#rows[@]}."
	done
}

az_tsv() {
	az "$@" --only-show-errors --output tsv | tr -d '\r'
}

info "Checking Azure CLI authentication..."
if ! az account list --only-show-errors --output none >/dev/null 2>&1; then
	die "Azure CLI authentication failed. Run 'az login' and try again."
fi

# Subscription and tenant
SUBSCRIPTION_ID="$(existing AZURE_SUBSCRIPTION_ID)"
SUBSCRIPTION_ROW=""
if [[ -n "$SUBSCRIPTION_ID" ]]; then
	SUBSCRIPTION_ROW="$(az_tsv account show --subscription "$SUBSCRIPTION_ID" \
		--query "join('${TSV_DELIMITER}', [id, tenantId, name, state])" 2>/dev/null || true)"
	[[ -n "$SUBSCRIPTION_ROW" ]] || warn "The subscription in the existing dotenv file is unavailable."
fi

if [[ -z "$SUBSCRIPTION_ROW" ]]; then
	SUBSCRIPTION_ROW="$(az_tsv account show \
		--query "join('${TSV_DELIMITER}', [id, tenantId, name, state])" 2>/dev/null || true)"
fi

if [[ -z "$SUBSCRIPTION_ROW" ]]; then
	if ! subscriptions_output="$(az_tsv account list \
		--query "[?state=='Enabled'].[id,tenantId,name,state]")"; then
		die "Could not list Azure subscriptions. Check Azure CLI authentication and network access."
	fi
	subscriptions=()
	while IFS= read -r row; do
		if [[ -n "$row" ]]; then
			subscriptions+=("$row")
		fi
	done <<<"$subscriptions_output"
	((${#subscriptions[@]} > 0)) || die "No enabled Azure subscriptions are available."
	if ((${#subscriptions[@]} == 1)); then
		SUBSCRIPTION_ROW="${subscriptions[0]}"
	else
		select_row "Available Azure subscriptions (ID | tenant | name | state):" "${subscriptions[@]}"
		SUBSCRIPTION_ROW="$SELECTED_ROW"
	fi
fi

IFS=$'\t' read -r SUBSCRIPTION_ID TENANT_ID SUBSCRIPTION_NAME SUBSCRIPTION_STATE <<<"$SUBSCRIPTION_ROW"
[[ "$SUBSCRIPTION_STATE" == "Enabled" ]] || die "Selected subscription is not enabled."
info "Using subscription: $SUBSCRIPTION_NAME ($SUBSCRIPTION_ID)"

# Foundry/OpenAI account. Existing values are hints and are validated first.
EXISTING_RESOURCE_GROUP="$(existing AZURE_RESOURCE_GROUP)"
EXISTING_FOUNDRY_RESOURCE="$(existing AZURE_FOUNDRY_RESOURCE)"
RESOURCE_GROUP="$EXISTING_RESOURCE_GROUP"
FOUNDRY_RESOURCE="$EXISTING_FOUNDRY_RESOURCE"
ACCOUNT_ROW=""
ACCOUNT_VERIFIED=false
if [[ -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" ]]; then
	ACCOUNT_ROW="$(az_tsv cognitiveservices account show \
		--subscription "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
		--name "$FOUNDRY_RESOURCE" \
		--query "join('${TSV_DELIMITER}', [name, resourceGroup, location, kind, not_null(properties.endpoint, '__NONE__'), not_null(properties.customSubDomainName, '__NONE__')])" \
		2>/dev/null || true)"
	if [[ -z "$ACCOUNT_ROW" ]]; then
		warn "The Foundry resource in the existing dotenv file is unavailable; its values will not be retained."
		RESOURCE_GROUP=""
		FOUNDRY_RESOURCE=""
	fi
fi

if [[ -z "$ACCOUNT_ROW" ]]; then
	if ! accounts_output="$(az_tsv cognitiveservices account list \
		--subscription "$SUBSCRIPTION_ID" \
		--query "[?kind=='AIServices' || kind=='OpenAI'].[name,resourceGroup,location,kind,not_null(properties.endpoint, '__NONE__'),not_null(properties.customSubDomainName, '__NONE__')]")"; then
		warn "Could not list Foundry resources; resource fields can be completed manually later."
		accounts_output=""
	fi
	accounts=()
	while IFS= read -r row; do
		if [[ -n "$row" ]]; then
			accounts+=("$row")
		fi
	done <<<"$accounts_output"
	if ((${#accounts[@]} == 1)); then
		ACCOUNT_ROW="${accounts[0]}"
		info "Found one compatible Foundry resource; selecting it automatically."
	elif ((${#accounts[@]} > 1)); then
		select_row "Foundry resources (name | resource group | region | kind | endpoint):" "${accounts[@]}"
		ACCOUNT_ROW="$SELECTED_ROW"
	else
		warn "No compatible Foundry resources were discovered automatically."
		prompt_optional "Foundry resource group" "$RESOURCE_GROUP"
		RESOURCE_GROUP="$REPLY"
		prompt_optional "Foundry resource name" "$FOUNDRY_RESOURCE"
		FOUNDRY_RESOURCE="$REPLY"
		if [[ -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" ]]; then
			ACCOUNT_ROW="$(az_tsv cognitiveservices account show \
				--subscription "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
				--name "$FOUNDRY_RESOURCE" \
				--query "join('${TSV_DELIMITER}', [name, resourceGroup, location, kind, not_null(properties.endpoint, '__NONE__'), not_null(properties.customSubDomainName, '__NONE__')])" \
				2>/dev/null || true)"
			[[ -n "$ACCOUNT_ROW" ]] || warn "That resource could not be read; retaining the supplied names for manual completion."
		fi
	fi
fi

REGION=""
ACCOUNT_KIND=""
ACCOUNT_ENDPOINT=""
CUSTOM_SUBDOMAIN=""
if [[ -n "$ACCOUNT_ROW" ]]; then
	ACCOUNT_VERIFIED=true
	IFS=$'\t' read -r FOUNDRY_RESOURCE RESOURCE_GROUP REGION ACCOUNT_KIND ACCOUNT_ENDPOINT CUSTOM_SUBDOMAIN <<<"$ACCOUNT_ROW"
elif [[ -n "$FOUNDRY_RESOURCE" ]]; then
	prompt_optional "Foundry resource region" ""
	REGION="$REPLY"
fi
[[ "$ACCOUNT_ENDPOINT" == "__NONE__" ]] && ACCOUNT_ENDPOINT=""
[[ "$CUSTOM_SUBDOMAIN" == "__NONE__" ]] && CUSTOM_SUBDOMAIN=""
case "$ACCOUNT_KIND" in
	AIServices|OpenAI|"") ;;
	*)
		warn "The selected resource kind '$ACCOUNT_KIND' may not support this benchmark; verify it manually."
		ACCOUNT_VERIFIED=false
		;;
esac

ENDPOINT_VALID=false
if [[ "$ACCOUNT_ENDPOINT" =~ ^https://[A-Za-z0-9-]+\.openai\.azure\.com/?$ ]]; then
	OPENAI_ENDPOINT="${ACCOUNT_ENDPOINT%/}/"
elif [[ -n "$CUSTOM_SUBDOMAIN" ]]; then
	OPENAI_ENDPOINT="https://${CUSTOM_SUBDOMAIN}.openai.azure.com/"
elif [[ -n "$FOUNDRY_RESOURCE" ]]; then
	OPENAI_ENDPOINT="https://${FOUNDRY_RESOURCE}.openai.azure.com/"
else
	OPENAI_ENDPOINT=""
fi
if [[ "$OPENAI_ENDPOINT" =~ ^https://[A-Za-z0-9-]+\.openai\.azure\.com/$ ]]; then
	ENDPOINT_VALID=true
elif [[ -n "$OPENAI_ENDPOINT" ]]; then
	warn "The OpenAI endpoint format could not be validated; clear or correct it manually."
fi
if [[ -n "$FOUNDRY_RESOURCE" ]]; then
	info "Using Foundry resource: $FOUNDRY_RESOURCE ($RESOURCE_GROUP, $REGION)"
else
	warn "No Foundry resource was selected; dependent discovery will be skipped."
fi

# Foundry project
EXISTING_FOUNDRY_PROJECT="$(existing AZURE_FOUNDRY_PROJECT)"
FOUNDRY_PROJECT="$EXISTING_FOUNDRY_PROJECT"
PROJECT_VALID=false
if [[ -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" && -n "$FOUNDRY_PROJECT" ]] && az cognitiveservices account project show \
	--subscription "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
	--name "$FOUNDRY_RESOURCE" --project-name "$FOUNDRY_PROJECT" \
	--only-show-errors --output none >/dev/null 2>&1; then
	PROJECT_VALID=true
fi

if [[ "$PROJECT_VALID" != true ]]; then
	if [[ -n "$FOUNDRY_PROJECT" ]]; then
		warn "The project in the existing dotenv file was not found; its value will not be retained."
		FOUNDRY_PROJECT=""
	fi
	projects_output=""
	if [[ -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" ]] && ! projects_output="$(az_tsv cognitiveservices account project list \
		--subscription "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
		--name "$FOUNDRY_RESOURCE" --query '[].[name,location]')"; then
		warn "Project discovery failed; enter the project name manually."
		projects_output=""
	fi
	projects=()
	while IFS= read -r row; do
		if [[ -n "$row" ]]; then
			projects+=("$row")
		fi
	done <<<"$projects_output"
	if ((${#projects[@]} == 1)); then
		IFS=$'\t' read -r FOUNDRY_PROJECT _ <<<"${projects[0]}"
		PROJECT_VALID=true
		info "Found one project; selecting it automatically."
	elif ((${#projects[@]} > 1)); then
		select_row "Foundry projects (name | region):" "${projects[@]}"
		IFS=$'\t' read -r FOUNDRY_PROJECT _ <<<"$SELECTED_ROW"
		PROJECT_VALID=true
	else
		warn "No projects were returned. The project can be completed manually later."
		prompt_optional "Foundry project name" ""
		FOUNDRY_PROJECT="$REPLY"
		if [[ -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" && -n "$FOUNDRY_PROJECT" ]] && az cognitiveservices account project show \
			--subscription "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
			--name "$FOUNDRY_RESOURCE" --project-name "$FOUNDRY_PROJECT" \
			--only-show-errors --output none >/dev/null 2>&1; then
			PROJECT_VALID=true
		else
			[[ -z "$FOUNDRY_PROJECT" ]] || warn "The supplied project could not be verified."
		fi
	fi
fi

# Model deployments. Each row contains:
# name, model name/version/format, SKU name/capacity, RAI policy, upgrade policy.
deployments_output=""
if [[ -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" ]] && ! deployments_output="$(az_tsv cognitiveservices account deployment list \
	--subscription "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
	--name "$FOUNDRY_RESOURCE" \
	--query "[].[name,properties.model.name,properties.model.version,properties.model.format,sku.name,sku.capacity,not_null(properties.raiPolicyName, '__NONE__'),not_null(properties.versionUpgradeOption, '__NONE__')]")"; then
	warn "Could not list model deployments; deployment fields will require manual completion."
	deployments_output=""
fi
deployments=()
while IFS= read -r row; do
	if [[ -n "$row" ]]; then
		deployments+=("$row")
	fi
done <<<"$deployments_output"

paygo_rows=()
ptu_rows=()
for row in "${deployments[@]}"; do
	IFS=$'\t' read -r _ _ _ _ row_sku _ _ _ <<<"$row"
	sku_lower="$(printf '%s' "$row_sku" | tr '[:upper:]' '[:lower:]')"
	if [[ "$sku_lower" == "globalstandard" ]]; then
		paygo_rows+=("$row")
	elif [[ "$sku_lower" == "globalprovisionedmanaged" ]]; then
		ptu_rows+=("$row")
	fi
done
if ((${#paygo_rows[@]} == 0)); then
	warn "No GlobalStandard PayGo deployment was found."
fi
if ((${#ptu_rows[@]} == 0)); then
	warn "No GlobalProvisionedManaged PTU deployment was found."
fi

deployments_are_comparable() {
	local paygo_row="$1" ptu_row="$2"
	local paygo_model paygo_version paygo_format paygo_rai paygo_upgrade
	local ptu_model ptu_version ptu_format ptu_rai ptu_upgrade
	IFS=$'\t' read -r _ paygo_model paygo_version paygo_format _ _ paygo_rai paygo_upgrade <<<"$paygo_row"
	IFS=$'\t' read -r _ ptu_model ptu_version ptu_format _ _ ptu_rai ptu_upgrade <<<"$ptu_row"
	[[ "$paygo_model" == "$ptu_model" ]] || return 1
	[[ "$paygo_version" == "$ptu_version" ]] || return 1
	[[ "$paygo_format" == "$ptu_format" ]] || return 1
	if [[ "$paygo_rai" != "__NONE__" && "$ptu_rai" != "__NONE__" && "$paygo_rai" != "$ptu_rai" ]]; then
		return 1
	fi
	if [[ "$paygo_upgrade" != "__NONE__" && "$ptu_upgrade" != "__NONE__" && "$paygo_upgrade" != "$ptu_upgrade" ]]; then
		return 1
	fi
	return 0
}

choose_deployment() {
	local type="$1" existing_name="$2"
	shift 2
	local -a choices=("$@")
	local row name
	CHOSEN_ROW=""
	if [[ -n "$existing_name" ]]; then
		for row in "${choices[@]}"; do
			IFS=$'\t' read -r name _ <<<"$row"
			if [[ "$name" == "$existing_name" ]]; then
				CHOSEN_ROW="$row"
				break
			fi
		done
		[[ -n "$CHOSEN_ROW" ]] || warn "Existing $type deployment '$existing_name' is unavailable."
	fi
	if [[ -z "$CHOSEN_ROW" ]]; then
		if ((${#choices[@]} == 1)); then
			CHOSEN_ROW="${choices[0]}"
		else
			select_row "Select the $type deployment (name | model | version | format | SKU | capacity | RAI policy | upgrade policy):" "${choices[@]}"
			CHOSEN_ROW="$SELECTED_ROW"
		fi
	fi
}

EXISTING_PAYGO_NAME="$(existing AZURE_DEPLOYMENT_GLOBAL_STANDARD)"
EXISTING_PTU_NAME="$(existing AZURE_DEPLOYMENT_PROVISIONED)"
PAYGO_NAME=""
PTU_NAME=""
PAYGO_SKU=""
PAYGO_CAPACITY=""
PTU_SKU=""
PTU_CAPACITY=""
MODEL_NAME=""
MODEL_VERSION=""
MODEL_FORMAT=""
PAYGO_RAI=""
PAYGO_UPGRADE=""
PTU_RAI=""
PTU_UPGRADE=""
PAIR_COMPATIBLE=false

comparable_paygo_rows=()
for paygo_row in "${paygo_rows[@]}"; do
	for ptu_row in "${ptu_rows[@]}"; do
		if deployments_are_comparable "$paygo_row" "$ptu_row"; then
			comparable_paygo_rows+=("$paygo_row")
			break
		fi
	done
done

if ((${#comparable_paygo_rows[@]} > 0)); then
	choose_deployment "Global Standard/PayGo" "$EXISTING_PAYGO_NAME" "${comparable_paygo_rows[@]}"
	PAYGO_ROW="$CHOSEN_ROW"
	IFS=$'\t' read -r PAYGO_NAME MODEL_NAME MODEL_VERSION MODEL_FORMAT PAYGO_SKU PAYGO_CAPACITY PAYGO_RAI PAYGO_UPGRADE <<<"$PAYGO_ROW"
	[[ "$PAYGO_RAI" == "__NONE__" ]] && PAYGO_RAI=""
	[[ "$PAYGO_UPGRADE" == "__NONE__" ]] && PAYGO_UPGRADE=""

	matching_ptu_rows=()
	for row in "${ptu_rows[@]}"; do
		if deployments_are_comparable "$PAYGO_ROW" "$row"; then
			matching_ptu_rows+=("$row")
		fi
	done
	choose_deployment "provisioned/PTU" "$EXISTING_PTU_NAME" "${matching_ptu_rows[@]}"
	PTU_ROW="$CHOSEN_ROW"
	IFS=$'\t' read -r PTU_NAME _ _ _ PTU_SKU PTU_CAPACITY PTU_RAI PTU_UPGRADE <<<"$PTU_ROW"
	[[ "$PTU_RAI" == "__NONE__" ]] && PTU_RAI=""
	[[ "$PTU_UPGRADE" == "__NONE__" ]] && PTU_UPGRADE=""
else
	warn "No fully comparable Global Standard/PTU pair was found; deployment fields will require manual review."
	if ((${#paygo_rows[@]} == 1)); then
		IFS=$'\t' read -r PAYGO_NAME MODEL_NAME MODEL_VERSION MODEL_FORMAT PAYGO_SKU PAYGO_CAPACITY PAYGO_RAI PAYGO_UPGRADE <<<"${paygo_rows[0]}"
		[[ "$PAYGO_RAI" == "__NONE__" ]] && PAYGO_RAI=""
		[[ "$PAYGO_UPGRADE" == "__NONE__" ]] && PAYGO_UPGRADE=""
	fi
	if ((${#ptu_rows[@]} == 1)); then
		IFS=$'\t' read -r PTU_NAME ptu_model ptu_version ptu_format PTU_SKU PTU_CAPACITY PTU_RAI PTU_UPGRADE <<<"${ptu_rows[0]}"
		[[ -n "$MODEL_NAME" ]] || MODEL_NAME="$ptu_model"
		[[ -n "$MODEL_VERSION" ]] || MODEL_VERSION="$ptu_version"
		[[ -n "$MODEL_FORMAT" ]] || MODEL_FORMAT="$ptu_format"
		[[ "$PTU_RAI" == "__NONE__" ]] && PTU_RAI=""
		[[ "$PTU_UPGRADE" == "__NONE__" ]] && PTU_UPGRADE=""
	fi
fi

if [[ -n "$PAYGO_RAI" && "$PAYGO_RAI" == "$PTU_RAI" ]]; then
	CONTENT_FILTER_POLICY="$PAYGO_RAI"
else
	CONTENT_FILTER_POLICY="$(template_default BENCH_CONTENT_FILTER_POLICY)"
	warn "Azure did not report one shared content-filter policy; using the template default '$CONTENT_FILTER_POLICY'."
fi
if [[ -n "$PAYGO_UPGRADE" && "$PAYGO_UPGRADE" == "$PTU_UPGRADE" ]]; then
	VERSION_UPGRADE_POLICY="$PAYGO_UPGRADE"
else
	VERSION_UPGRADE_POLICY="$(template_default BENCH_VERSION_UPGRADE_POLICY)"
	warn "Azure did not report one shared version-upgrade policy; using the template default '$VERSION_UPGRADE_POLICY'."
fi
if [[ "$PAYGO_SKU" == "GlobalStandard" && "$PTU_SKU" == "GlobalProvisionedManaged" ]]; then
	ROUTING_SCOPE="global for both deployment types"
else
	ROUTING_SCOPE=""
fi

if [[ -n "$PAYGO_NAME" && -n "$PTU_NAME" \
	&& -n "$MODEL_NAME" && -n "$MODEL_VERSION" && -n "$MODEL_FORMAT" \
	&& "$PAYGO_SKU" == "GlobalStandard" \
	&& "$PTU_SKU" == "GlobalProvisionedManaged" \
	&& -n "$PAYGO_RAI" && "$PAYGO_RAI" == "$PTU_RAI" \
	&& -n "$PAYGO_UPGRADE" && "$PAYGO_UPGRADE" == "$PTU_UPGRADE" ]]; then
	PAIR_COMPATIBLE=true
fi

CLIENT_LOCATION=""
prompt_optional "Benchmark client location (for latency interpretation)" ""
CLIENT_LOCATION="$REPLY"

# Verify the exact data-plane route used by app.py. The token exists only in
# memory and is passed through one child process environment.
API_VERSION_VERIFIED=false
if [[ -n "$OPENAI_ENDPOINT" ]]; then
	info "Verifying the non-inference OpenAI v1 models endpoint..."
	ACCESS_TOKEN="$(az account get-access-token --subscription "$SUBSCRIPTION_ID" \
		--resource https://cognitiveservices.azure.com/ --query accessToken \
		--only-show-errors --output tsv 2>/dev/null || true)"
else
	ACCESS_TOKEN=""
fi
if [[ -n "$OPENAI_ENDPOINT" && -n "$ACCESS_TOKEN" ]]; then
	if AZURE_VERIFY_TOKEN="$ACCESS_TOKEN" AZURE_VERIFY_ENDPOINT="$OPENAI_ENDPOINT" \
		"$PYTHON" - <<'PY'
import os
import urllib.error
import urllib.request

url = os.environ["AZURE_VERIFY_ENDPOINT"].rstrip("/") + "/openai/v1/models"
request = urllib.request.Request(
	url,
	headers={"Authorization": "Bearer " + os.environ["AZURE_VERIFY_TOKEN"]},
)

class NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, request, file_pointer, code, message, headers, new_url):
		return None

try:
	opener = urllib.request.build_opener(NoRedirect)
	with opener.open(request, timeout=20) as response:
		raise SystemExit(0 if 200 <= response.status < 300 else 1)
except (urllib.error.URLError, TimeoutError):
	raise SystemExit(1)
PY
	then
		API_VERSION_VERIFIED=true
		info "OpenAI v1 endpoint verified."
	else
		warn "The v1 endpoint could not be verified. Check data-plane RBAC and network access."
	fi
else
	warn "The OpenAI v1 endpoint could not be verified; complete or verify it manually."
fi
unset ACCESS_TOKEN

printf '\nConfiguration summary\n' >&2
printf '  Subscription:  %s (%s)\n' "$SUBSCRIPTION_NAME" "$SUBSCRIPTION_ID" >&2
printf '  Tenant:        %s\n' "$TENANT_ID" >&2
printf '  Resource:      %s / %s\n' "$RESOURCE_GROUP" "$FOUNDRY_RESOURCE" >&2
printf '  Project:       %s\n' "$FOUNDRY_PROJECT" >&2
printf '  Endpoint:      %s\n' "$OPENAI_ENDPOINT" >&2
printf '  Region:        %s\n' "$REGION" >&2
printf '  Model:         %s %s (%s)\n' "$MODEL_NAME" "$MODEL_VERSION" "$MODEL_FORMAT" >&2
printf '  PayGo:         %s - %s, capacity %s\n' "$PAYGO_NAME" "$PAYGO_SKU" "$PAYGO_CAPACITY" >&2
printf '  PTU:           %s - %s, capacity %s\n' "$PTU_NAME" "$PTU_SKU" "$PTU_CAPACITY" >&2
printf '  Content filter:%s\n' " $CONTENT_FILTER_POLICY" >&2
printf '  Upgrade policy:%s\n' " $VERSION_UPGRADE_POLICY" >&2
printf '  Routing:       %s\n' "$ROUTING_SCOPE" >&2
printf '  Client:        %s\n' "$CLIENT_LOCATION" >&2
printf '  v1 verified:   %s\n\n' "$API_VERSION_VERIFIED" >&2

manual_actions=()
add_manual_action() {
	if [[ -z "$2" ]]; then
		manual_actions+=("$1")
	fi
}
add_manual_action "AZURE_FOUNDRY_PROJECT" "$FOUNDRY_PROJECT"
add_manual_action "AZURE_OPENAI_ENDPOINT" "$OPENAI_ENDPOINT"
add_manual_action "AZURE_RESOURCE_GROUP" "$RESOURCE_GROUP"
add_manual_action "AZURE_FOUNDRY_RESOURCE" "$FOUNDRY_RESOURCE"
add_manual_action "AZURE_DEPLOYMENT_GLOBAL_STANDARD" "$PAYGO_NAME"
add_manual_action "AZURE_DEPLOYMENT_PROVISIONED" "$PTU_NAME"
add_manual_action "BENCH_SKU_GLOBAL_STANDARD_NAME" "$PAYGO_SKU"
add_manual_action "BENCH_SKU_GLOBAL_STANDARD_CAPACITY" "$PAYGO_CAPACITY"
add_manual_action "BENCH_SKU_PROVISIONED_NAME" "$PTU_SKU"
add_manual_action "BENCH_SKU_PROVISIONED_CAPACITY" "$PTU_CAPACITY"
add_manual_action "BENCH_MODEL_NAME" "$MODEL_NAME"
add_manual_action "BENCH_MODEL_VERSION" "$MODEL_VERSION"
add_manual_action "BENCH_MODEL_FORMAT" "$MODEL_FORMAT"
add_manual_action "BENCH_REGION" "$REGION"
add_manual_action "BENCH_CLIENT_LOCATION" "$CLIENT_LOCATION"
add_manual_action "BENCH_CONTENT_FILTER_POLICY" "$CONTENT_FILTER_POLICY"
add_manual_action "BENCH_VERSION_UPGRADE_POLICY" "$VERSION_UPGRADE_POLICY"
add_manual_action "BENCH_ROUTING_SCOPE" "$ROUTING_SCOPE"
if [[ "$ACCOUNT_VERIFIED" != true && -n "$RESOURCE_GROUP" && -n "$FOUNDRY_RESOURCE" ]]; then
	manual_actions+=("AZURE_RESOURCE_GROUP / AZURE_FOUNDRY_RESOURCE (verify resource identity)")
fi
if [[ "$PROJECT_VALID" != true && -n "$FOUNDRY_PROJECT" ]]; then
	manual_actions+=("AZURE_FOUNDRY_PROJECT (verify project identity)")
fi
if [[ "$ENDPOINT_VALID" != true && -n "$OPENAI_ENDPOINT" ]]; then
	manual_actions+=("AZURE_OPENAI_ENDPOINT (correct invalid endpoint format)")
fi
if [[ "$PAIR_COMPATIBLE" != true && -n "$PAYGO_NAME" && -n "$PTU_NAME" ]]; then
	manual_actions+=("deployment pair compatibility (verify model/version/format/policies)")
fi
if [[ "$API_VERSION_VERIFIED" != true ]]; then
	manual_actions+=("AZURE_OPENAI_API_VERSION_VERIFIED (verify v1, then set true)")
fi

if ((${#manual_actions[@]} == 0)); then
	info "Managed to fill and verify every resource-specific setting."
else
	warn "Managed to fill the discoverable settings, but these could not be completed automatically:"
	for action in "${manual_actions[@]}"; do
		printf '  - %s\n' "$action" >&2
	done
	warn "They will remain blank or unverified in the dotenv file and must be completed manually."
fi
printf '\n' >&2

confirm "Write this configuration to $OUTPUT_FILE?" || die "Cancelled; no files were changed."

output_dir="$(dirname -- "$OUTPUT_FILE")"
mkdir -p -- "$output_dir"
TEMP_FILE="$(mktemp "$output_dir/.env.tmp.XXXXXX")"

export CFG_AZURE_OPENAI_ENDPOINT="$OPENAI_ENDPOINT"
export CFG_AZURE_OPENAI_API_VERSION="v1"
export CFG_AZURE_OPENAI_API_VERSION_VERIFIED="$API_VERSION_VERIFIED"
export CFG_AZURE_SUBSCRIPTION_ID="$SUBSCRIPTION_ID"
export CFG_AZURE_TENANT_ID="$TENANT_ID"
export CFG_AZURE_RESOURCE_GROUP="$RESOURCE_GROUP"
export CFG_AZURE_FOUNDRY_RESOURCE="$FOUNDRY_RESOURCE"
export CFG_AZURE_FOUNDRY_PROJECT="$FOUNDRY_PROJECT"
export CFG_AZURE_DEPLOYMENT_GLOBAL_STANDARD="$PAYGO_NAME"
export CFG_AZURE_DEPLOYMENT_PROVISIONED="$PTU_NAME"
export CFG_BENCH_SKU_GLOBAL_STANDARD_NAME="$PAYGO_SKU"
export CFG_BENCH_SKU_GLOBAL_STANDARD_CAPACITY="$PAYGO_CAPACITY"
export CFG_BENCH_SKU_PROVISIONED_NAME="$PTU_SKU"
export CFG_BENCH_SKU_PROVISIONED_CAPACITY="$PTU_CAPACITY"
export CFG_BENCH_MODEL_NAME="$MODEL_NAME"
export CFG_BENCH_MODEL_VERSION="$MODEL_VERSION"
export CFG_BENCH_MODEL_FORMAT="$MODEL_FORMAT"
export CFG_BENCH_REGION="$REGION"
export CFG_BENCH_CLIENT_LOCATION="$CLIENT_LOCATION"
export CFG_BENCH_CONTENT_FILTER_POLICY="$CONTENT_FILTER_POLICY"
export CFG_BENCH_VERSION_UPGRADE_POLICY="$VERSION_UPGRADE_POLICY"
export CFG_BENCH_ROUTING_SCOPE="$ROUTING_SCOPE"

# Create a new dotenv from the tracked template when none exists. When one does
# exist, retain unmanaged benchmark tuning but replace every managed resource,
# deployment, SKU, and provenance key below, including unresolved blank values.
BASE_FILE="$TEMPLATE_FILE"
[[ -r "$OUTPUT_FILE" ]] && BASE_FILE="$OUTPUT_FILE"
export CFG_BASE_FILE="$BASE_FILE" CFG_TEMP_FILE="$TEMP_FILE"
"$PYTHON" - <<'PY'
import os
import re

keys = (
	"AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION",
	"AZURE_OPENAI_API_VERSION_VERIFIED", "AZURE_SUBSCRIPTION_ID",
	"AZURE_TENANT_ID", "AZURE_RESOURCE_GROUP", "AZURE_FOUNDRY_RESOURCE",
	"AZURE_FOUNDRY_PROJECT", "AZURE_DEPLOYMENT_GLOBAL_STANDARD",
	"AZURE_DEPLOYMENT_PROVISIONED", "BENCH_SKU_GLOBAL_STANDARD_NAME",
	"BENCH_SKU_GLOBAL_STANDARD_CAPACITY", "BENCH_SKU_PROVISIONED_NAME",
	"BENCH_SKU_PROVISIONED_CAPACITY", "BENCH_MODEL_NAME",
	"BENCH_MODEL_VERSION", "BENCH_MODEL_FORMAT", "BENCH_REGION",
	"BENCH_CLIENT_LOCATION", "BENCH_CONTENT_FILTER_POLICY",
	"BENCH_VERSION_UPGRADE_POLICY", "BENCH_ROUTING_SCOPE",
)
values = {key: os.environ["CFG_" + key] for key in keys}

def encode(value: str) -> str:
	if any(ch in value for ch in "\r\n\0"):
		raise ValueError("dotenv values must be single-line text")
	if not value:
		return ""
	if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
		return value
	return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

with open(os.environ["CFG_BASE_FILE"], encoding="utf-8-sig") as stream:
	lines = stream.readlines()

seen = set()
result = []
assignment = re.compile(r"^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)(\s*)=")
sku_assignment = re.compile(r"^BENCH_SKU_[A-Z0-9_]+_(?:NAME|CAPACITY)$")
for line in lines:
	match = assignment.match(line)
	if not match:
		result.append(line)
		continue
	key = match.group(2)
	if key.startswith("AZURE_DEPLOYMENT_") and key not in values:
		continue
	if sku_assignment.fullmatch(key) and key not in values:
		continue
	if key in values:
		if key in seen:
			continue
		result.append(f"{match.group(1)}{key}={encode(values[key])}\n")
		seen.add(key)
	else:
		result.append(line)
if result and not result[-1].endswith("\n"):
	result[-1] += "\n"
for key in keys:
	if key not in seen:
		result.append(f"{key}={encode(values[key])}\n")

with open(os.environ["CFG_TEMP_FILE"], "w", encoding="utf-8", newline="\n") as stream:
	stream.writelines(result)
PY

if [[ -e "$OUTPUT_FILE" ]]; then
	BACKUP_FILE="$OUTPUT_FILE.backup.$(date -u +%Y%m%dT%H%M%SZ).$$"
	cp -p -- "$OUTPUT_FILE" "$BACKUP_FILE"
	info "Backed up the existing dotenv file to $BACKUP_FILE"
fi
chmod 600 "$TEMP_FILE" 2>/dev/null || true
mv -f -- "$TEMP_FILE" "$OUTPUT_FILE"
TEMP_FILE=""
info "Wrote $OUTPUT_FILE"
if ((${#manual_actions[@]} == 0)); then
	info "All settings were discovered and verified."
else
	warn "The dotenv file was saved with partial findings. Complete the manual items listed above."
fi
info "Next: run 'python app.py --dry-run' to see any remaining configuration requirements."
