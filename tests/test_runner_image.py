from pathlib import Path


def test_runner_image_uses_oci_base_and_preinstalls_runner() -> None:
    dockerfile = Path("sandbox-image/Dockerfile").read_text()
    assert "FROM ubuntu-2204-base" in dockerfile
    assert "actions/runner/releases/latest" in dockerfile
    assert "download.docker.com/linux/ubuntu" in dockerfile
    assert "docker-ce" in dockerfile
    assert "systemctl enable containerd.service docker.service" in dockerfile
    assert "ENV TENSORLAKE_CLI_VERSION=cli-v0.5.130" in dockerfile
    assert "ARG TENSORLAKE_CLI_VERSION" not in dockerfile
    assert 'TENSORLAKE_VERSION="${TENSORLAKE_CLI_VERSION}"' in dockerfile
    assert 'test "$(tl --version)" = "tl ${TENSORLAKE_CLI_VERSION#cli-v}"' in dockerfile

    build_script = Path("scripts/build-runner-image.sh").read_text()
    assert "--build-arg" not in build_script
    assert "TENSORLAKE_RUNNER_CLI_VERSION" not in build_script


def test_orchestrator_image_excludes_local_build_state() -> None:
    ignored = set(Path(".dockerignore").read_text().splitlines())
    assert {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"} <= ignored


def test_orchestrate_app_invokes_preinstalled_runner_directly() -> None:
    app_source = Path("github_runner_orchestrator/app.py").read_text()
    assert '"/opt/actions-runner/run.sh"' in app_source
    assert '"--jitconfig", jit.encoded_jit_config' in app_source


def test_webhook_uses_raw_http_body_and_request_headers() -> None:
    app_source = Path("github_runner_orchestrator/app.py").read_text()
    assert "async def github_runner_webhook(payload: HttpBody)" in app_source
    assert "headers = RequestContext.get().headers" in app_source
    assert 'headers.get("X-Hub-Signature-256")' in app_source
    assert "payload: File" not in app_source


def test_runner_always_waits_for_systemd_managed_docker() -> None:
    app_source = Path("github_runner_orchestrator/app.py").read_text()
    assert "await _wait_for_docker(sandbox)" in app_source
    assert 'start_process("dockerd"' not in app_source


def test_optional_runner_configuration_is_not_injected_as_secrets() -> None:
    app_source = Path("github_runner_orchestrator/app.py").read_text()
    for old_secret in (
        "DOCKER_RUNNER_LABEL",
        "TENSORLAKE_RUNNER_CPUS",
        "TENSORLAKE_RUNNER_DISK_MB",
        "TENSORLAKE_RUNNER_IMAGE",
        "TENSORLAKE_RUNNER_MEMORY_MB",
        "TENSORLAKE_RUNNER_TIMEOUT_SECS",
    ):
        assert old_secret not in app_source
