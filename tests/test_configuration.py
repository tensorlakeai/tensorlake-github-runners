import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def _upgrade_test_environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uv",
        """#!/bin/sh
if [ "${1:-}" = "run" ]; then
    shift
    if [ "${1:-}" = "--no-sync" ]; then
        shift
    fi
    if [ "${1:-}" = "python" ]; then
        shift
        exec python3 "$@"
    fi
    exec "$@"
fi
exit 0
""",
    )

    environment = os.environ.copy()
    environment.pop("TENSORLAKE_API_KEY", None)
    environment.pop("TENSORLAKE_ORGANIZATION_ID", None)
    environment.pop("TENSORLAKE_PROJECT_ID", None)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )
    return fake_bin, environment


def _run_upgrade(environment: dict[str, str], user_input: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/configure-github-org.sh", "--upgrade"],
        input=user_input,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def test_configuration_script_uses_clis_and_configures_org_webhook() -> None:
    script = Path("scripts/configure-github-org.sh").read_text()
    assert "command -v gh" in script
    assert "command -v tl" in script
    assert "command -v uv" in script
    assert "gh auth login" in script
    assert "tl login" in script
    assert "Use this Tensorlake organization and project?" in script
    assert "user/memberships/orgs/${github_org}" in script
    assert "uv sync --locked --python 3.11" in script
    assert "uv run --no-sync python" in script
    assert "tl secrets set --env-file" in script
    assert """output.write(f'{name}="{value}"\\n')""" in script
    assert 'read -r -s -p "${prompt}: " value' in script
    assert 'prompt_secret_required tensorlake_api_key "Tensorlake project API key"' in script
    assert 'tensorlake_secret_exists "TENSORLAKE_API_KEY"' in script
    assert "output.write(f'TENSORLAKE_API_KEY=\"{value}\"\\n')" in script
    assert "ensure_tensorlake_api_key_secret" in script
    assert "tl app deploy" in script
    assert "--resume-from-step-6" in script
    assert '"events": ["workflow_job"]' in script
    assert '"orgs/${organization}/hooks"' in script
    assert "Step %s of 7" in script
    assert "Webhook -> Active: DESELECTED / OFF" in script
    assert "It is NOT the webhook receiver" in script

    main = script.split("main() {", maxsplit=1)[1]
    store_tensorlake_api_key = main.index("ensure_tensorlake_api_key_secret")
    store_secrets = main.index('tl secrets set --env-file "${secret_env}"')
    deploy_application = main.index('deploy_application "${deploy_log}"')
    create_webhook = main.index(
        'configure_organization_hook "${github_org}" "${endpoint_url}" "${webhook_secret}"'
    )
    assert store_tensorlake_api_key < store_secrets < deploy_application < create_webhook


def test_upgrade_rejects_an_authenticated_cli_without_project_context(tmp_path: Path) -> None:
    fake_bin, environment = _upgrade_test_environment(tmp_path)
    _write_executable(
        fake_bin / "tl",
        """#!/bin/sh
if [ "${1:-}" = "whoami" ]; then
    if [ "${2:-}" = "-o" ]; then
        printf '%s\\n' '{"personalAccessToken":{"token":"<REDACTED>"}}'
    else
        printf '%s\\n' 'Credentials: Personal Access Token'
    fi
    exit 0
fi
exit 64
""",
    )
    result = _run_upgrade(environment, "n\n")

    assert result.returncode != 0
    assert "Tensorlake organization and project are not selected" in result.stderr
    assert "Upgrade this Tensorlake installation?" not in result.stdout


def test_upgrade_exports_the_prompted_project_api_key(tmp_path: Path) -> None:
    fake_bin, environment = _upgrade_test_environment(tmp_path)
    _write_executable(
        fake_bin / "tl",
        """#!/bin/sh
case "${1:-}" in
  whoami)
    if [ "${2:-}" = "-o" ]; then
        printf '%s\\n' '{"personalAccessToken":{"token":"<REDACTED>","organizationId":"org_T7MwTdFrBRHdpQPWf8Jdh","projectId":"project_Bn6BzggtncBfqQPFHJC8T"}}'
    else
        printf '%s\\n' 'Organization: org_T7MwTdFrBRHdpQPWf8Jdh'
        printf '%s\\n' 'Project: project_Bn6BzggtncBfqQPFHJC8T'
    fi
    exit 0
    ;;
  secrets)
    if [ "${2:-}" = "ls" ]; then
        printf '%s\\n' 'permission denied' >&2
        exit 1
    fi
    if [ "${2:-}" = "set" ]; then
        if [ "${TENSORLAKE_API_KEY:-}" != "tl_project_key_test" ]; then
            printf '%s\\n' 'project API key is not available' >&2
            exit 77
        fi
        exit 0
    fi
    ;;
  sbx)
    exit 0
    ;;
  app)
    printf '%s\\n' '🌍 Public endpoint: https://example.invalid/webhook'
    exit 0
    ;;
esac
exit 64
""",
    )
    result = _run_upgrade(environment, "\ntl_project_key_test\n")

    assert result.returncode == 0, result.stderr
    assert "Upgrade complete." in result.stdout
    assert "tl_project_key_test" not in result.stdout
    assert "tl_project_key_test" not in result.stderr


def test_readme_explains_the_webhook_and_deployment_order() -> None:
    readme = Path("README.md").read_text()
    assert "GitHub App vs. webhook" in readme
    assert "Webhook → Active disabled" in readme
    assert "created after deploy, using the endpoint the deploy returns" in readme
    assert "Redeploy after changing a secret" in readme


def test_self_hosted_workflow_builds_on_tensorlake_runner() -> None:
    workflow = Path(".github/workflows/build-reference.yml").read_text()
    assert "runs-on: [self-hosted, tensorlake, tensorlake-small]" in workflow
    assert "uses: ./actions/setup-uv-cache" in workflow
    assert "uv build" in workflow
    assert "uv run --no-sync pytest -q" in workflow
    assert "docker run --rm hello-world" in workflow


def test_application_image_installs_dependencies_with_uv() -> None:
    application = Path("github_runner_orchestrator/app.py").read_text()
    assert "ghcr.io/astral-sh/uv:python3.11-bookworm-slim" in application
    assert "uv pip install --system" in application
    assert '"TENSORLAKE_API_KEY"' in application


def test_deployment_uses_repository_root_and_current_public_endpoint_api() -> None:
    entrypoint = Path("app.py").read_text()
    application = Path("github_runner_orchestrator/app.py").read_text()
    deploy_helper = Path("scripts/tensorlake-deploy").read_text()

    assert "from github_runner_orchestrator.app import" in entrypoint
    assert '@application(allow=["unauthenticated_requests"])' in application
    assert "from __future__ import annotations" not in application
    assert "application_manifest_json" in deploy_helper
    assert "/applications/public/{endpoint_id}" in deploy_helper
