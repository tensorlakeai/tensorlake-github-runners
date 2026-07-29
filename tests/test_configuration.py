from pathlib import Path


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
    assert "tl app deploy" in script
    assert "--resume-from-step-6" in script
    assert '"events": ["workflow_job"]' in script
    assert '"orgs/${organization}/hooks"' in script
    assert "Step %s of 7" in script
    assert "Webhook -> Active: DESELECTED / OFF" in script
    assert "It is NOT the webhook receiver" in script

    main = script.split("main() {", maxsplit=1)[1]
    store_secrets = main.index('tl secrets set --env-file "${secret_env}"')
    deploy_application = main.index('deploy_application "${deploy_log}"')
    create_webhook = main.index(
        'configure_organization_hook "${github_org}" "${endpoint_url}" "${webhook_secret}"'
    )
    assert store_secrets < deploy_application < create_webhook


def test_readme_explains_the_webhook_and_deployment_order() -> None:
    readme = Path("README.md").read_text()
    assert "Why there is no webhook/deployment cycle" in readme
    assert "Before deployment; its own webhook is disabled" in readme
    assert "After deployment returns the endpoint URL" in readme
    assert "Tensorlake secrets exist at the project level" in readme


def test_self_hosted_workflow_builds_on_tensorlake_runner() -> None:
    workflow = Path(".github/workflows/build-reference.yml").read_text()
    assert "runs-on: [self-hosted, tensorlake, tensorlake-medium]" in workflow
    assert "astral-sh/setup-uv@v8" in workflow
    assert "uv sync --locked --all-extras" in workflow
    assert "uv build" in workflow
    assert "uv run --no-sync pytest -q" in workflow
    assert "docker run --rm hello-world" in workflow


def test_application_image_installs_dependencies_with_uv() -> None:
    application = Path("github_runner_orchestrator/app.py").read_text()
    assert "ghcr.io/astral-sh/uv:python3.11-bookworm-slim" in application
    assert "uv pip install --system" in application


def test_deployment_uses_repository_root_and_current_public_endpoint_api() -> None:
    entrypoint = Path("app.py").read_text()
    application = Path("github_runner_orchestrator/app.py").read_text()
    deploy_helper = Path("scripts/tensorlake-deploy").read_text()

    assert "from github_runner_orchestrator.app import" in entrypoint
    assert '@application(allow=["unauthenticated_requests"])' in application
    assert "from __future__ import annotations" not in application
    assert "application_manifest_json" in deploy_helper
    assert "/applications/public/{endpoint_id}" in deploy_helper
