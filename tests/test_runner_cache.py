import asyncio
from types import SimpleNamespace

import pytest

import github_runner_orchestrator.app as app_module
from github_runner_orchestrator.cache import CACHE_MOUNT_PATH

# The mount and unmount run under sudo so the mount daemon can raise its
# open-file limit; tl still presents the volume to the invoking tl-user.
PRESERVE_ENV = (
    "--preserve-env=TENSORLAKE_GIT_TOKEN,TENSORLAKE_GIT_USERNAME,"
    "TENSORLAKE_PROJECT_ID,TENSORLAKE_API_URL"
)


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
        self.started_processes: list[tuple] = []
        self.run_calls: list[tuple[str, list[str], dict | None, str | None, str | None]] = []
        self.terminated = False
        self.mount_checks = 0
        self.mount_process_status = "running"
        # The detached runner process exits immediately in tests so the
        # supervision loop finishes on its first poll without sleeping.
        self.runner_process_status = "exited"
        self.runner_exit_code = 0
        self.killed_processes: list[str] = []

    async def start_process(
        self, command, args, env=None, name=None, restart=None, user=None, working_dir=None
    ):
        self.started_processes.append((command, args, env, name, user, restart, working_dir))
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
        if name == "tensorlake-cache-mount":
            return SimpleNamespace(
                status=self.mount_process_status,
                exit_code=1 if self.mount_process_status != "running" else None,
            )
        return SimpleNamespace(status=self.runner_process_status, exit_code=self.runner_exit_code)

    async def get_stdout(self, name):
        return SimpleNamespace(lines=["runner stdout"])

    async def get_stderr(self, name):
        return SimpleNamespace(lines=["mount failed"])

    async def kill_process(self, name):
        self.killed_processes.append(name)

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
            "sudo",
            [
                PRESERVE_ENV,
                "/usr/local/bin/tl",
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
            None,
        )
    ]
    assert sandbox.mount_checks == 2
    assert any(call[0] == "python3" for call in sandbox.run_calls)
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
    # The unmount runs under sudo, mirroring the sudo mount.
    unmount_call = next(call for call in sandbox.run_calls if call[0] == "sudo")
    assert docker_call[4] == app_module.RUNNER_USER
    assert sync_call[4] == app_module.RUNNER_USER
    assert unmount_call[1][:4] == [PRESERVE_ENV, "/usr/local/bin/tl", "fs", "unmount"]
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


def test_mount_cache_rejects_a_mounted_but_unusable_filesystem() -> None:
    class ProbeFailureSandbox(FakeSandbox):
        async def run(self, command, args, env=None, working_dir=None, timeout=None, user=None):
            result = await super().run(command, args, env, working_dir, timeout, user)
            if command == "python3":
                return SimpleNamespace(
                    exit_code=1,
                    stdout="",
                    stderr="Permission denied",
                )
            return result

    sandbox = ProbeFailureSandbox()

    with pytest.raises(RuntimeError, match="authenticated write/read/delete readiness probe") as e:
        asyncio.run(
            app_module._mount_cache_filesystem(
                sandbox,
                "github-actions-cache-example",
                {},
            )
        )

    assert "Permission denied" in str(e.value)
    assert "mount failed" in str(e.value)


def test_prepare_cache_mount_remints_and_retries_after_failed_io_probe(monkeypatch) -> None:
    class RetrySandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.active_mount = False
            self.probes = 0

        async def start_process(self, *args, **kwargs):
            self.active_mount = True
            return await super().start_process(*args, **kwargs)

        async def run(self, command, args, env=None, working_dir=None, timeout=None, user=None):
            self.run_calls.append((command, args, env, working_dir, user))
            if command == "mountpoint":
                return SimpleNamespace(exit_code=0 if self.active_mount else 1)
            if command == "python3":
                self.probes += 1
                if self.probes == 1:
                    return SimpleNamespace(exit_code=1, stdout="", stderr="Input/output error")
            if command == "sudo" and args[:2] == ["/usr/bin/fusermount3", "-uz"]:
                self.active_mount = False
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        async def kill_process(self, name):
            await super().kill_process(name)
            self.active_mount = False

    sandbox = RetrySandbox()
    logger = FakeLogger()
    credentials = iter(["first-token", "second-token"])
    monkeypatch.setattr(app_module, "logger", logger)
    monkeypatch.setattr(app_module, "CACHE_MOUNT_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        app_module,
        "cache_mount_environment",
        lambda _name, api_key: {
            "TENSORLAKE_GIT_TOKEN": next(credentials),
            "TENSORLAKE_PROJECT_ID": "project_example",
        },
    )

    environment = asyncio.run(
        app_module._prepare_cache_mount(
            sandbox,
            "github-actions-cache-example",
            "example/repository",
            "project-api-key",
        )
    )

    assert environment["TENSORLAKE_GIT_TOKEN"] == "second-token"
    assert sandbox.probes == 2
    assert sandbox.killed_processes == ["tensorlake-cache-mount"]
    assert [process[3] for process in sandbox.started_processes] == [
        "tensorlake-cache-mount",
        "tensorlake-cache-mount-retry-2",
    ]
    failed_attempt = next(
        fields
        for message, fields in logger.warning_events
        if message == "Repository cache mount attempt failed"
    )
    assert failed_attempt["will_retry"] is True


def test_settle_cache_writes_retries_safe_unmount(monkeypatch) -> None:
    class SettlingSandbox:
        def __init__(self) -> None:
            self.unmount_attempts = 0

        async def run(self, command, args, env=None, timeout=None, user=None):
            if command == "sync":
                assert user == app_module.RUNNER_USER
                return SimpleNamespace(exit_code=0, stdout="", stderr="")

            assert command == "sudo"
            assert args == [
                PRESERVE_ENV,
                "/usr/local/bin/tl",
                "fs",
                "unmount",
                CACHE_MOUNT_PATH,
            ]
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
    monkeypatch.setenv(app_module.RUNNER_TENSORLAKE_API_KEY_SECRET, "project-api-key")
    monkeypatch.setattr(app_module, "_github_credentials", lambda: SimpleNamespace())
    monkeypatch.setattr(
        app_module,
        "get_installation_token",
        lambda _credentials: "installation-token",
    )
    monkeypatch.setattr(
        app_module,
        "generate_repository_jit_config",
        lambda **_kwargs: SimpleNamespace(encoded_jit_config="jit-config"),
    )

    # The runner is supervised by polling; each poll writes a progress update
    # via the request context. Provide a no-op context so the supervision loop
    # runs without a live Tensorlake request.
    fake_context = SimpleNamespace(progress=SimpleNamespace(update=lambda *a, **k: None))
    monkeypatch.setattr(app_module.RequestContext, "get", staticmethod(lambda: fake_context))

    if isinstance(cache_result, Exception):

        def provision_cache(_repository, api_key):
            assert api_key == "project-api-key"
            raise cache_result
    else:

        def provision_cache(_repository, api_key):
            assert api_key == "project-api-key"
            return cache_result

    monkeypatch.setattr(app_module, "ensure_cache_filesystem", provision_cache)
    monkeypatch.setattr(
        app_module,
        "cache_mount_environment",
        lambda _name, api_key: {
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

    async def create_sandbox(**kwargs):
        assert kwargs["api_key"] == "project-api-key"
        return sandbox

    monkeypatch.setattr(AsyncSandbox, "create", staticmethod(create_sandbox))


def _started_runner(sandbox):
    return next(
        process
        for process in sandbox.started_processes
        if process[0] == "/opt/actions-runner/run.sh"
    )


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

    runner_process = _started_runner(sandbox)
    # (command, args, env, name, user, restart, working_dir)
    assert runner_process[2] == {
        "HOME": app_module.RUNNER_HOME,
        "LOGNAME": app_module.RUNNER_USER,
        "USER": app_module.RUNNER_USER,
        "TENSORLAKE_CACHE_DIR": CACHE_MOUNT_PATH,
    }
    assert runner_process[3] == app_module.RUNNER_PROCESS_NAME
    assert runner_process[4] == app_module.RUNNER_USER
    assert result["cache_filesystem"] == "github-actions-cache-example"
    assert result["cache_mounted"] is True
    assert result["exit_code"] == 0
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

    runner_process = _started_runner(sandbox)
    assert runner_process[2] == {
        "HOME": app_module.RUNNER_HOME,
        "LOGNAME": app_module.RUNNER_USER,
        "USER": app_module.RUNNER_USER,
    }
    assert runner_process[4] == app_module.RUNNER_USER
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
