from pathlib import Path


def test_runner_image_uses_oci_base_and_preinstalls_runner() -> None:
    dockerfile = Path("sandbox-image/Dockerfile").read_text()
    assert "FROM ubuntu-2204-base" in dockerfile
    assert "actions/runner/releases/latest" in dockerfile
    assert "download.docker.com/linux/ubuntu" in dockerfile
    assert "docker-ce" in dockerfile
    assert "systemctl enable containerd.service docker.service" in dockerfile


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
