import asyncio
from types import SimpleNamespace

import github_runner_orchestrator.app as app_module
from github_runner_orchestrator.cache import CACHE_MOUNT_PATH


class FakeLogger:
    def __init__(self) -> None:
        self.info_events: list[tuple[str, dict]] = []
        self.warning_events: list[tuple[str, dict]] = []

    def info(self, message: str, **fields) -> None:
        self.info_events.append((message, fields))

    def warning(self, message: str, **fields) -> None:
        self.warning_events.append((message, fields))


class FakeSandbox:
    sandbox_id = "sandbox_1"

    def __init__(self) -> None:
        self.started_processes: list[tuple[str, list[str], dict, str | None]] = []
        self.run_calls: list[tuple[str, list[str], dict | None, str | None]] = []
        self.terminated = False
        self.mount_checks = 0

    async def start_process(self, command, args, env, name):
        self.started_processes.append((command, args, env, name))
        return SimpleNamespace(pid=101)

    async def run(self, command, args, env=None, working_dir=None, timeout=None):
        self.run_calls.append((command, args, env, working_dir))
        if command == "mountpoint":
            self.mount_checks += 1
            return SimpleNamespace(exit_code=0 if self.mount_checks >= 2 else 1)
        return SimpleNamespace(exit_code=0, stdout="runner output", stderr="")

    async def terminate(self):
        self.terminated = True


def test_mount_cache_starts_scoped_foreground_mount_and_waits_until_ready() -> None:
    sandbox = FakeSandbox()
    mount_environment = {
        "TENSORLAKE_GIT_TOKEN": "scoped-token",
        "TENSORLAKE_GIT_USERNAME": "scoped-user",
        "TENSORLAKE_PROJECT_ID": "project_example",
    }

    asyncio.run(
        app_module._mount_cache_filesystem(
            sandbox,
            "github-actions-cache-example",
            mount_environment,
        )
    )

    assert sandbox.started_processes == [
        (
            "/usr/local/bin/tl",
            [
                "fs",
                "mount",
                "--foreground",
                "github-actions-cache-example",
                CACHE_MOUNT_PATH,
            ],
            mount_environment,
            "tensorlake-cache-mount",
        )
    ]
    assert sandbox.mount_checks == 2
    assert "TENSORLAKE_API_KEY" not in mount_environment


def _configure_runner_dependencies(monkeypatch, sandbox, logger, cache_result):
    monkeypatch.setattr(app_module, "logger", logger)
    monkeypatch.setattr(app_module, "_github_credentials", lambda: SimpleNamespace())
    monkeypatch.setattr(
        app_module,
        "get_installation_token",
        lambda _credentials: "installation-token",
    )
    monkeypatch.setattr(
        app_module,
        "generate_org_jit_config",
        lambda **_kwargs: SimpleNamespace(encoded_jit_config="jit-config"),
    )

    if isinstance(cache_result, Exception):

        def provision_cache(_repository):
            raise cache_result
    else:

        def provision_cache(_repository):
            return cache_result

    monkeypatch.setattr(app_module, "ensure_cache_filesystem", provision_cache)
    monkeypatch.setattr(
        app_module,
        "cache_mount_environment",
        lambda _name: {
            "TENSORLAKE_GIT_TOKEN": "scoped-token",
            "TENSORLAKE_PROJECT_ID": "project_example",
        },
    )

    async def wait_for_docker(_sandbox):
        return None

    async def settle_cache_writes(_sandbox):
        return None

    monkeypatch.setattr(app_module, "_wait_for_docker", wait_for_docker)
    monkeypatch.setattr(app_module, "_settle_cache_writes", settle_cache_writes)

    from tensorlake.sandbox import AsyncSandbox

    async def create_sandbox(**_kwargs):
        return sandbox

    monkeypatch.setattr(AsyncSandbox, "create", staticmethod(create_sandbox))


def test_runner_exposes_only_generic_cache_root_after_successful_mount(monkeypatch) -> None:
    sandbox = FakeSandbox()
    logger = FakeLogger()
    _configure_runner_dependencies(
        monkeypatch,
        sandbox,
        logger,
        "github-actions-cache-example",
    )

    result = asyncio.run(
        app_module.run_github_runner._original_function(
            {
                "org": "example-org",
                "run_id": 123,
                "labels": ["self-hosted", "tensorlake"],
                "repository": "example-org/example-repository",
                "workflow_job_id": 456,
            }
        )
    )

    runner_call = next(
        call for call in sandbox.run_calls if call[0] == "/opt/actions-runner/run.sh"
    )
    assert runner_call[2] == {
        "RUNNER_ALLOW_RUNASROOT": "1",
        "TENSORLAKE_CACHE_DIR": CACHE_MOUNT_PATH,
    }
    assert result["cache_filesystem"] == "github-actions-cache-example"
    assert result["cache_mounted"] is True
    assert sandbox.terminated is True
    assert logger.warning_events == []


def test_runner_logs_provisioning_failure_and_continues_without_cache(monkeypatch) -> None:
    sandbox = FakeSandbox()
    logger = FakeLogger()
    _configure_runner_dependencies(
        monkeypatch,
        sandbox,
        logger,
        RuntimeError("volume service unavailable"),
    )

    result = asyncio.run(
        app_module.run_github_runner._original_function(
            {
                "org": "example-org",
                "run_id": 123,
                "labels": ["self-hosted", "tensorlake"],
                "repository": "example-org/example-repository",
                "workflow_job_id": 456,
            }
        )
    )

    runner_call = next(
        call for call in sandbox.run_calls if call[0] == "/opt/actions-runner/run.sh"
    )
    assert runner_call[2] == {"RUNNER_ALLOW_RUNASROOT": "1"}
    assert result["cache_filesystem"] is None
    assert result["cache_mounted"] is False
    assert logger.warning_events == [
        (
            "Running job without persistent cache because volume provisioning failed",
            {
                "repository": "example-org/example-repository",
                "stage": "cache_provision",
                "error_type": "RuntimeError",
                "error": "volume service unavailable",
                "exc_info": True,
            },
        )
    ]
