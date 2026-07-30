import asyncio
from types import SimpleNamespace

import github_runner_orchestrator.app as app_module
import pytest
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
        self.started_processes: list[tuple[str, list[str], dict, str | None, str | None, dict]] = []
        self.run_calls: list[tuple[str, list[str], dict | None, str | None, str | None]] = []
        self.terminated = False
        self.mount_checks = 0
        self.mount_process_status = "running"

    async def start_process(self, command, args, env, name, restart, user=None):
        self.started_processes.append((command, args, env, name, user, restart))
        return SimpleNamespace(pid=101)

    async def run(
        self,
        command,
        args,
        env=None,
        working_dir=None,
        timeout=None,
        user=None,
    ):
        self.run_calls.append((command, args, env, working_dir, user))
        if command == "mountpoint":
            self.mount_checks += 1
            return SimpleNamespace(exit_code=0 if self.mount_checks >= 2 else 1)
        return SimpleNamespace(exit_code=0, stdout="runner output", stderr="")

    async def get_process(self, name):
        assert name == "tensorlake-cache-mount"
        return SimpleNamespace(
            status=self.mount_process_status,
            exit_code=1 if self.mount_process_status != "running" else None,
        )

    async def get_stderr(self, name):
        assert name == "tensorlake-cache-mount"
        return SimpleNamespace(lines=["mount failed"])

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
            {
                **mount_environment,
                "HOME": app_module.RUNNER_HOME,
                "LOGNAME": app_module.RUNNER_USER,
                "USER": app_module.RUNNER_USER,
            },
            "tensorlake-cache-mount",
            app_module.RUNNER_USER,
            {"policy": "never"},
        )
    ]
    assert sandbox.mount_checks == 2
    assert all(
        call[4] == app_module.RUNNER_USER for call in sandbox.run_calls if call[0] == "mountpoint"
    )
    assert "TENSORLAKE_API_KEY" not in mount_environment


def test_runner_support_checks_and_cache_sync_use_runner_user(monkeypatch) -> None:
    sandbox = FakeSandbox()

    async def exercise_helpers() -> None:
        await app_module._wait_for_docker(sandbox)
        await app_module._settle_cache_writes(sandbox, {})

    asyncio.run(exercise_helpers())

    docker_call = next(call for call in sandbox.run_calls if call[0] == "docker")
    sync_call = next(call for call in sandbox.run_calls if call[0] == "sync")
    unmount_call = next(call for call in sandbox.run_calls if call[0] == "/usr/local/bin/tl")
    assert docker_call[4] == app_module.RUNNER_USER
    assert sync_call[4] == app_module.RUNNER_USER
    assert unmount_call[4] == app_module.RUNNER_USER


def test_mount_cache_reports_process_failure_immediately() -> None:
    sandbox = FakeSandbox()
    sandbox.mount_process_status = "exited"

    with pytest.raises(RuntimeError, match="mount failed"):
        asyncio.run(
            app_module._mount_cache_filesystem(
                sandbox,
                "github-actions-cache-example",
                {},
            )
        )


def test_settle_cache_writes_retries_safe_unmount(monkeypatch) -> None:
    class SettlingSandbox:
        def __init__(self) -> None:
            self.unmount_attempts = 0

        async def run(self, command, args, env=None, timeout=None, user=None):
            if command == "sync":
                assert user == app_module.RUNNER_USER
                return SimpleNamespace(exit_code=0, stdout="", stderr="")

            assert command == "/usr/local/bin/tl"
            assert args == ["fs", "unmount", CACHE_MOUNT_PATH]
            assert env == {
                "TENSORLAKE_GIT_TOKEN": "scoped-token",
                "HOME": app_module.RUNNER_HOME,
                "LOGNAME": app_module.RUNNER_USER,
                "USER": app_module.RUNNER_USER,
            }
            assert user == app_module.RUNNER_USER
            self.unmount_attempts += 1
            if self.unmount_attempts == 1:
                return SimpleNamespace(
                    exit_code=1,
                    stdout="",
                    stderr="unsaved work remains",
                )
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    sandbox = SettlingSandbox()
    monkeypatch.setattr(app_module, "CACHE_UNMOUNT_RETRY_SECONDS", 0)
    monkeypatch.setattr(app_module, "CACHE_UNMOUNT_MAX_ATTEMPTS", 2)

    asyncio.run(
        app_module._settle_cache_writes(
            sandbox,
            {"TENSORLAKE_GIT_TOKEN": "scoped-token"},
        )
    )

    assert sandbox.unmount_attempts == 2


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

    async def settle_cache_writes(_sandbox, _mount_environment):
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
        "HOME": app_module.RUNNER_HOME,
        "LOGNAME": app_module.RUNNER_USER,
        "USER": app_module.RUNNER_USER,
        "TENSORLAKE_CACHE_DIR": CACHE_MOUNT_PATH,
    }
    assert runner_call[4] == app_module.RUNNER_USER
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
    assert runner_call[2] == {
        "HOME": app_module.RUNNER_HOME,
        "LOGNAME": app_module.RUNNER_USER,
        "USER": app_module.RUNNER_USER,
    }
    assert runner_call[4] == app_module.RUNNER_USER
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
