#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

info() {
  printf '\n==> %s\n' "$*"
}

phase() {
  local number="$1"
  local title="$2"
  printf '\n============================================================\n'
  printf 'Step %s of 7: %s\n' "${number}" "${title}"
  printf '============================================================\n'
}

print_installation_plan() {
  printf '\nInstallation order (the wizard performs these steps in sequence):\n'
  printf '  1. Install local tools and sync the Python environment.\n'
  printf '  2. Authenticate the GitHub and Tensorlake CLIs.\n'
  printf '  3. Create and install a GitHub App for API credentials.\n'
  printf '     IMPORTANT: Disable the GitHub App webhook; it does not need a URL.\n'
  printf '  4. Store the GitHub App credentials and a generated webhook secret in Tensorlake.\n'
  printf '     Tensorlake secrets exist independently of a deployment, so this happens first.\n'
  printf '  5. Build the reusable GitHub runner sandbox image.\n'
  printf '  6. Deploy the Tensorlake application and obtain its public endpoint URL.\n'
  printf '  7. Create a separate GitHub organization webhook using that endpoint and secret.\n'
  printf '\nThere is no webhook URL dependency in step 3: the GitHub App is used only to mint\n'
  printf 'runner credentials. The organization webhook created in step 7 delivers events.\n'
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

confirm() {
  local prompt="$1"
  local answer
  read -r -p "${prompt} [Y/n] " answer
  [[ -z "${answer}" || "${answer}" =~ ^[Yy]$ ]]
}

prompt_required() {
  local variable_name="$1"
  local prompt="$2"
  local value
  while true; do
    read -r -p "${prompt}: " value
    if [[ -n "${value}" ]]; then
      printf -v "${variable_name}" '%s' "${value}"
      return
    fi
    printf 'A value is required.\n'
  done
}

as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "Installing packages requires root access or sudo."
  fi
}

install_gh() {
  command -v gh >/dev/null 2>&1 && return

  info "GitHub CLI is not installed"
  confirm "Install GitHub CLI now?" || die "GitHub CLI is required."

  if command -v brew >/dev/null 2>&1; then
    brew install gh
  elif command -v apt-get >/dev/null 2>&1; then
    as_root apt-get update
    as_root apt-get install -y gh
  elif command -v dnf >/dev/null 2>&1; then
    as_root dnf install -y gh
  elif command -v yum >/dev/null 2>&1; then
    as_root yum install -y gh
  else
    die "No supported package manager found. Install gh from https://cli.github.com/ and rerun this script."
  fi

  command -v gh >/dev/null 2>&1 || die "GitHub CLI installation did not put gh on PATH."
}

install_tl() {
  command -v tl >/dev/null 2>&1 && return

  info "Tensorlake CLI is not installed"
  confirm "Install Tensorlake CLI now?" || die "Tensorlake CLI is required."
  command -v curl >/dev/null 2>&1 || die "curl is required to install the Tensorlake CLI."

  local installer="${TMP_DIR}/install-tensorlake.sh"
  curl -fsSL https://tensorlake.ai/install -o "${installer}"
  sh "${installer}"
  hash -r

  command -v tl >/dev/null 2>&1 || die "Tensorlake CLI installation did not put tl on PATH. Open a new shell and rerun this script."
}

install_uv() {
  command -v uv >/dev/null 2>&1 && return

  info "uv is not installed"
  confirm "Install uv now?" || die "uv is required."
  command -v curl >/dev/null 2>&1 || die "curl is required to install uv."

  curl -LsSf https://astral.sh/uv/install.sh | sh
  hash -r

  command -v uv >/dev/null 2>&1 || die "uv installation did not put uv on PATH. Open a new shell and rerun this script."
}

ensure_authentication() {
  if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    info "Authenticate GitHub CLI"
    gh auth login --hostname github.com --web
  fi

  if ! tl whoami >/dev/null 2>&1; then
    info "Authenticate Tensorlake CLI"
    tl login
  fi

  printf '\nGitHub CLI identity:\n'
  gh auth status --hostname github.com
  printf '\nTensorlake destination:\n'
  tl whoami
  printf '\nAll secrets, the runner image, and the application will be created in the\n'
  printf 'Tensorlake organization and project shown above.\n'
  confirm "Use this Tensorlake organization and project?" || \
    die "Select the intended Tensorlake context, then rerun this wizard."
}

ensure_project_environment() {
  info "Install Python and sync the reference application dependencies with uv"
  uv python install 3.11
  uv sync --locked --python 3.11
}

generate_webhook_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

find_hook_id() {
  local organization="$1"
  local endpoint_url="$2"
  local hooks_file="${TMP_DIR}/hooks.json"
  gh api --paginate --slurp "orgs/${organization}/hooks?per_page=100" >"${hooks_file}"
  uv run --no-sync python - "${hooks_file}" "${endpoint_url}" <<'PY'
import json
import sys

pages = json.load(open(sys.argv[1], encoding="utf-8"))
for page in pages:
    for hook in page:
        if hook.get("config", {}).get("url") == sys.argv[2]:
            print(hook["id"])
            raise SystemExit(0)
PY
}

write_hook_payload() {
  local endpoint_url="$1"
  local webhook_secret="$2"
  local payload_file="$3"
  ENDPOINT_URL="${endpoint_url}" WEBHOOK_SECRET="${webhook_secret}" \
    uv run --no-sync python - "${payload_file}" <<'PY'
import json
import os
import sys

payload = {
    "name": "web",
    "active": True,
    "events": ["workflow_job"],
    "config": {
        "url": os.environ["ENDPOINT_URL"],
        "content_type": "json",
        "secret": os.environ["WEBHOOK_SECRET"],
        "insecure_ssl": "0",
    },
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(payload, output)
PY
  chmod 600 "${payload_file}"
}

configure_organization_hook() {
  local organization="$1"
  local endpoint_url="$2"
  local webhook_secret="$3"
  local payload_file="${TMP_DIR}/hook.json"
  local hook_id

  if ! gh api --silent "orgs/${organization}/hooks?per_page=1"; then
    info "GitHub needs permission to manage organization webhooks"
    gh auth refresh --hostname github.com --scopes admin:org_hook
    gh api --silent "orgs/${organization}/hooks?per_page=1"
  fi

  write_hook_payload "${endpoint_url}" "${webhook_secret}" "${payload_file}"
  hook_id="$(find_hook_id "${organization}" "${endpoint_url}")"
  if [[ -n "${hook_id}" ]]; then
    gh api --silent --method PATCH "orgs/${organization}/hooks/${hook_id}" --input "${payload_file}"
    printf 'Updated organization webhook %s.\n' "${hook_id}"
  else
    gh api --silent --method POST "orgs/${organization}/hooks" --input "${payload_file}"
    printf 'Created organization webhook.\n'
  fi
}

main() {
  cd "${ROOT_DIR}"
  printf 'Tensorlake GitHub runner organization setup\n'
  printf 'This wizard performs the complete installation without requiring a webhook URL up front.\n'
  print_installation_plan

  phase 1 "Prepare local tools and the Python environment"
  install_gh
  install_tl
  install_uv
  ensure_project_environment

  local github_org github_role github_app_client_id github_app_installation_id private_key_path
  local runner_group_id webhook_secret private_key secret_env deploy_log endpoint_url

  phase 2 "Authenticate GitHub and Tensorlake"
  ensure_authentication

  phase 3 "Create and install the credentials-only GitHub App"
  prompt_required github_org "GitHub organization"
  gh api --silent "orgs/${github_org}" || die "Cannot access GitHub organization '${github_org}'."
  github_role="$(gh api "user/memberships/orgs/${github_org}" --jq .role 2>/dev/null)" || \
    die "Cannot verify your membership in '${github_org}'. Authenticate gh with organization access and rerun."
  [[ "${github_role}" == "admin" ]] || \
    die "GitHub organization owner access is required; your role in '${github_org}' is '${github_role}'."
  printf 'Confirmed that the active GitHub user is an owner of %s.\n' "${github_org}"

  printf '\nThe GitHub App is used only to authenticate GitHub API calls that create ephemeral runners.\n'
  printf 'It is NOT the webhook receiver. A separate organization webhook will be created in step 7.\n\n'
  printf 'Open: https://github.com/organizations/%s/settings/apps\n' "${github_org}"
  printf 'Create a new GitHub App with these settings:\n'
  printf '  - GitHub App name: any unique, recognizable name.\n'
  printf '  - Homepage URL: this repository or your organization page.\n'
  printf '  - Webhook -> Active: DESELECTED / OFF.\n'
  printf '    Do not enter a webhook URL or webhook secret on the GitHub App.\n'
  printf '  - Organization permissions -> Self-hosted runners: Read and write.\n'
  printf '  - All other permissions: No access unless your organization requires otherwise.\n'
  printf '  - Installation scope: Only on this account.\n\n'
  printf 'After creating the app:\n'
  printf '  1. Generate and download a private key (.pem).\n'
  printf '  2. Install the app on the %s organization.\n' "${github_org}"
  printf '  3. Copy the Client ID from the app settings (not the numeric App ID).\n'
  printf '  4. Open the installed app and copy the numeric installation ID from the URL:\n'
  printf '     https://github.com/organizations/%s/settings/installations/<INSTALLATION_ID>\n\n' "${github_org}"
  confirm "Is the GitHub App created, installed, and its webhook disabled?" || \
    die "Finish the GitHub App steps above, then rerun this wizard."

  prompt_required github_app_client_id "GitHub App Client ID (not App ID)"
  prompt_required github_app_installation_id "GitHub App installation ID"
  [[ "${github_app_installation_id}" =~ ^[0-9]+$ ]] || die "GitHub App installation ID must be numeric."
  prompt_required private_key_path "Path to the GitHub App private key (.pem)"
  private_key_path="${private_key_path/#\~/${HOME}}"
  [[ -f "${private_key_path}" ]] || die "Private key file not found: ${private_key_path}"

  read -r -p "GitHub runner group ID [1]: " runner_group_id
  runner_group_id="${runner_group_id:-1}"
  [[ "${runner_group_id}" =~ ^[0-9]+$ ]] || die "Runner group ID must be numeric."

  printf '\nConfiguration summary:\n'
  printf '  GitHub organization: %s\n' "${github_org}"
  printf '  GitHub App client ID: %s\n' "${github_app_client_id}"
  printf '  GitHub App installation ID: %s\n' "${github_app_installation_id}"
  printf '  GitHub App webhook: disabled (credentials only)\n'
  printf '  Event source: organization webhook created after deployment\n'
  printf '  GitHub runner group ID: %s\n' "${runner_group_id}"
  confirm "Continue with Tensorlake secret storage, image build, deployment, and webhook creation?" || exit 0

  phase 4 "Store secrets in Tensorlake before deployment"
  printf 'Tensorlake stores these secrets independently of the application deployment.\n'
  printf 'The generated GITHUB_WEBHOOK_SECRET is for the organization webhook in step 7,\n'
  printf 'not for the disabled webhook on the GitHub App. The same value is stored now and\n'
  printf 'sent to GitHub only after the deployment provides an endpoint URL.\n'
  webhook_secret="$(generate_webhook_secret)"
  private_key="$(<"${private_key_path}")"
  secret_env="${TMP_DIR}/secrets.env"
  GITHUB_WEBHOOK_SECRET="${webhook_secret}" \
    GITHUB_APP_CLIENT_ID="${github_app_client_id}" \
    GITHUB_APP_INSTALLATION_ID="${github_app_installation_id}" \
    GITHUB_APP_PRIVATE_KEY="${private_key}" \
    RUNNER_GROUP_ID="${runner_group_id}" \
    uv run --no-sync python - "${secret_env}" <<'PY'
import os
import sys

private_key = os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\r\n", "\n").replace("\n", "\\n")
values = {
    "GITHUB_WEBHOOK_SECRET": os.environ["GITHUB_WEBHOOK_SECRET"],
    "GITHUB_APP_CLIENT_ID": os.environ["GITHUB_APP_CLIENT_ID"],
    "GITHUB_APP_INSTALLATION_ID": os.environ["GITHUB_APP_INSTALLATION_ID"],
    "GITHUB_APP_PRIVATE_KEY": private_key,
    "RUNNER_GROUP_ID": os.environ["RUNNER_GROUP_ID"],
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    for name, value in values.items():
        output.write(f"{name}={value}\n")
PY
  chmod 600 "${secret_env}"
  unset private_key

  tl secrets set --env-file "${secret_env}"
  printf 'Stored the GitHub credentials, runner group ID, and webhook secret in Tensorlake.\n'

  phase 5 "Build the Tensorlake runner sandbox image"
  "${ROOT_DIR}/scripts/build-runner-image.sh"

  phase 6 "Deploy the Tensorlake application and obtain its webhook URL"
  printf 'The deployment can now read the secrets stored in step 4.\n'
  deploy_log="${TMP_DIR}/deploy.log"
  if ! tl app deploy "${ROOT_DIR}/github_runner_orchestrator/app.py" 2>&1 | tee "${deploy_log}"; then
    die "Application deployment failed."
  fi
  endpoint_url="$(sed -n 's/^🌍 Public endpoint: //p' "${deploy_log}" | tail -n 1)"
  [[ -n "${endpoint_url}" ]] || die "Deployment did not report a public endpoint URL. Ensure the SDK and CLI include public endpoint support."
  printf '\nTensorlake application endpoint: %s\n' "${endpoint_url}"
  printf 'This URL belongs on the organization webhook, not on the GitHub App.\n'

  phase 7 "Create or update the GitHub organization webhook"
  printf 'The wizard will subscribe this separate webhook to workflow_job events and use\n'
  printf 'the exact secret already stored in Tensorlake in step 4.\n'
  configure_organization_hook "${github_org}" "${endpoint_url}" "${webhook_secret}"
  unset webhook_secret

  printf '\nSetup complete.\n'
  printf '  Organization: %s\n' "${github_org}"
  printf '  GitHub App: installed for runner API credentials; app webhook disabled\n'
  printf '  Organization webhook: active for workflow_job events\n'
  printf '  Organization webhook URL: %s\n' "${endpoint_url}"
  printf '  Runner labels: self-hosted, tensorlake, and optionally tensorlake-small, tensorlake-medium, or tensorlake-large\n'
  printf '\nNext step: run a workflow with runs-on: [self-hosted, tensorlake] to verify the installation.\n'
}

main "$@"
