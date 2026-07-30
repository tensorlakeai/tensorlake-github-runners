import asyncio
import os

from tensorlake.applications import (
    HttpBody,
    Image,
    Logger,
    RequestContext,
    application,
    function,
)

from github_runner_orchestrator.cache import (
    CACHE_MOUNT_PATH,
    CACHE_UNMOUNT_MAX_ATTEMPTS,
    CACHE_UNMOUNT_RETRY_SECONDS,
    cache_mount_environment,
    ensure_cache_filesystem,
)
from github_runner_orchestrator.github import (
    build_runner_name,
    generate_org_jit_config,
    get_installation_token,
)
from github_runner_orchestrator.models import AppCredentials, RunnerRequest
from github_runner_orchestrator.resources import resources_for_labels
from github_runner_orchestrator.webhook import (
    match_runner_request,
    parse_workflow_job_event,
    verify_signature,
)

app_image = Image(
    name="github-runner-orchestrator",
    base_image="ghcr.io/astral-sh/uv:python3.11-bookworm-slim",
).run("uv pip install --system 'PyJWT[crypto]>=2.8.0' requests 'tensorlake>=0.5.92'")
logger = Logger.get_logger(module="github_runner_orchestrator")

REQUIRED_RUNNER_LABEL = "tensorlake"
RUNNER_IMAGE = "github-actions-runner"
RUNNER_USER = "tl-user"
RUNNER_HOME = f"/home/{RUNNER_USER}"
RUNNER_TIMEOUT_SECS = 7200
CACHE_MOUNT_TIMEOUT_SECS = 30
GITHUB_ORG_OVERRIDE: str | None = None


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _github_credentials() -> AppCredentials:
    return AppCredentials(
        app_client_id=os.environ["GITHUB_APP_CLIENT_ID"],
        installation_id=os.environ["GITHUB_APP_INSTALLATION_ID"],
        private_key=os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\\n", "\n"),
    )


def _runner_request_from_dict(data: dict) -> RunnerRequest:
    return RunnerRequest(
        org=data["org"],
        run_id=data.get("run_id"),
        labels=list(data["labels"]),
        repository=data.get("repository"),
        workflow_job_id=data.get("workflow_job_id"),
    )


async def _wait_for_docker(sandbox) -> None:
    for _ in range(30):
        docker_info = await sandbox.run(
            "docker",
            ["info"],
            timeout=10,
            user=RUNNER_USER,
        )
        if docker_info.exit_code == 0:
            return
        await asyncio.sleep(1)

    raise RuntimeError("Docker's systemd service did not become ready in the sandbox")


async def _settle_cache_writes(
    sandbox,
    mount_environment: dict[str, str],
) -> None:
    sync_result = await sandbox.run(
        "sync",
        [],
        timeout=30,
        user=RUNNER_USER,
    )
    if sync_result.exit_code != 0:
        logger.warning(
            "Cache sync command failed",
            stage="cache_settle",
            exit_code=sync_result.exit_code,
            output=sync_result.stderr or sync_result.stdout,
        )

    last_output = ""
    for attempt in range(CACHE_UNMOUNT_MAX_ATTEMPTS):
        unmount_result = await sandbox.run(
            "/usr/local/bin/tl",
            ["fs", "unmount", CACHE_MOUNT_PATH],
            env={
                **mount_environment,
                "HOME": RUNNER_HOME,
                "LOGNAME": RUNNER_USER,
                "USER": RUNNER_USER,
            },
            timeout=30,
            user=RUNNER_USER,
        )
        if unmount_result.exit_code == 0:
            return

        last_output = unmount_result.stderr or unmount_result.stdout
        if attempt + 1 < CACHE_UNMOUNT_MAX_ATTEMPTS:
            await asyncio.sleep(CACHE_UNMOUNT_RETRY_SECONDS)

    raise RuntimeError(
        f"Cloud Volume at {CACHE_MOUNT_PATH} did not autosave and unmount "
        f"after {CACHE_UNMOUNT_MAX_ATTEMPTS} attempts: {last_output}"
    )


async def _mount_cache_filesystem(
    sandbox,
    file_system_name: str,
    mount_environment: dict[str, str],
) -> None:
    process_environment = {
        **mount_environment,
        "HOME": RUNNER_HOME,
        "LOGNAME": RUNNER_USER,
        "USER": RUNNER_USER,
    }
    await sandbox.start_process(
        "/usr/local/bin/tl",
        [
            "fs",
            "mount",
            "--foreground",
            file_system_name,
            CACHE_MOUNT_PATH,
        ],
        env=process_environment,
        user=RUNNER_USER,
        name="tensorlake-cache-mount",
        restart={"policy": "never"},
    )

    for _ in range(CACHE_MOUNT_TIMEOUT_SECS):
        mounted = await sandbox.run(
            "mountpoint",
            ["-q", CACHE_MOUNT_PATH],
            timeout=10,
            user=RUNNER_USER,
        )
        if mounted.exit_code == 0:
            return

        mount_process = await sandbox.get_process("tensorlake-cache-mount")
        if mount_process.status != "running":
            stderr = await sandbox.get_stderr("tensorlake-cache-mount")
            output = "\n".join(stderr.lines).strip()[-2000:]
            exit_code = mount_process.exit_code
            process_status = getattr(
                mount_process.status,
                "value",
                mount_process.status,
            )
            raise RuntimeError(
                f"Cloud Volume {file_system_name!r} mount process exited "
                f"with status {process_status} and exit code {exit_code}: "
                f"{output or 'no stderr output'}"
            )
        await asyncio.sleep(1)

    raise RuntimeError(
        f"Cloud Volume {file_system_name!r} did not mount at {CACHE_MOUNT_PATH} "
        f"within {CACHE_MOUNT_TIMEOUT_SECS} seconds"
    )


@function(
    image=app_image,
    timeout=7200,
    secrets=[
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "RUNNER_GROUP_ID",
        "TENSORLAKE_API_KEY",
        "TENSORLAKE_ORGANIZATION_ID",
        "TENSORLAKE_PROJECT_ID",
    ],
)
async def run_github_runner(request_data: dict) -> dict:
    from tensorlake.sandbox import AsyncSandbox

    request = _runner_request_from_dict(request_data)
    credentials = _github_credentials()
    installation_token = await asyncio.to_thread(get_installation_token, credentials)
    runner_name = build_runner_name(request.run_id)
    runner_group_id = _env_int("RUNNER_GROUP_ID", 1)
    resources = resources_for_labels(request.labels)

    jit = await asyncio.to_thread(
        generate_org_jit_config,
        installation_token=installation_token,
        org=request.org,
        runner_name=runner_name,
        runner_group_id=runner_group_id,
        labels=request.labels,
    )

    cache_filesystem_name = None
    if request.repository:
        try:
            cache_filesystem_name = await asyncio.to_thread(
                ensure_cache_filesystem,
                request.repository,
            )
            logger.info(
                "Repository cache volume is ready",
                repository=request.repository,
                cache_filesystem=cache_filesystem_name,
                stage="cache_provision",
            )
        except Exception as error:
            logger.warning(
                "Running job without persistent cache because volume provisioning failed",
                repository=request.repository,
                stage="cache_provision",
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )

    sandbox = await AsyncSandbox.create(
        image=RUNNER_IMAGE,
        cpus=resources.cpus,
        memory_mb=resources.memory_mb,
        disk_mb=resources.disk_mb,
        timeout_secs=RUNNER_TIMEOUT_SECS,
    )
    logger.info(
        "Runner sandbox created",
        repository=request.repository,
        sandbox_id=sandbox.sandbox_id,
        runner_name=runner_name,
        cpus=resources.cpus,
        memory_mb=resources.memory_mb,
        disk_mb=resources.disk_mb,
        runner_user=RUNNER_USER,
    )

    cache_mounted = False
    mount_environment = None
    try:
        await _wait_for_docker(sandbox)

        if cache_filesystem_name:
            try:
                mount_environment = await asyncio.to_thread(
                    cache_mount_environment,
                    cache_filesystem_name,
                )
                await _mount_cache_filesystem(
                    sandbox,
                    cache_filesystem_name,
                    mount_environment,
                )
                cache_mounted = True
                logger.info(
                    "Repository cache volume mounted",
                    repository=request.repository,
                    sandbox_id=sandbox.sandbox_id,
                    cache_filesystem=cache_filesystem_name,
                    cache_mount_path=CACHE_MOUNT_PATH,
                    stage="cache_mount",
                )
            except Exception as error:
                logger.warning(
                    "Running job without persistent cache because volume mounting failed",
                    repository=request.repository,
                    sandbox_id=sandbox.sandbox_id,
                    cache_filesystem=cache_filesystem_name,
                    stage="cache_mount",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )

        runner_environment = {
            "HOME": RUNNER_HOME,
            "LOGNAME": RUNNER_USER,
            "USER": RUNNER_USER,
        }
        if cache_mounted:
            runner_environment["TENSORLAKE_CACHE_DIR"] = CACHE_MOUNT_PATH

        result = await sandbox.run(
            "/opt/actions-runner/run.sh",
            ["--jitconfig", jit.encoded_jit_config],
            env=runner_environment,
            working_dir="/opt/actions-runner",
            user=RUNNER_USER,
        )
        logger.info(
            "GitHub Actions runner exited",
            repository=request.repository,
            sandbox_id=sandbox.sandbox_id,
            runner_name=runner_name,
            runner_user=RUNNER_USER,
            exit_code=int(result.exit_code or 0),
            cache_mounted=cache_mounted,
        )
        return {
            "sandbox_id": sandbox.sandbox_id,
            "runner_name": runner_name,
            "exit_code": int(result.exit_code or 0),
            "cache_filesystem": cache_filesystem_name,
            "cache_mounted": cache_mounted,
            "stdout_tail": (result.stdout or "")[-4000:],
            "stderr_tail": (result.stderr or "")[-4000:],
        }
    finally:
        if cache_mounted:
            try:
                await _settle_cache_writes(sandbox, mount_environment or {})
            except Exception as error:
                logger.warning(
                    "Failed to autosave and unmount persistent cache before sandbox termination",
                    repository=request.repository,
                    sandbox_id=sandbox.sandbox_id,
                    cache_filesystem=cache_filesystem_name,
                    stage="cache_settle",
                    error_type=type(error).__name__,
                    error=str(error),
                    exc_info=True,
                )
        await sandbox.terminate()


@application(allow=["unauthenticated_requests"])
@function(
    image=app_image,
    secrets=["GITHUB_WEBHOOK_SECRET"],
)
async def github_runner_webhook(payload: HttpBody) -> dict:
    raw_body_bytes = payload.content
    headers = RequestContext.get().headers
    if headers.get("X-GitHub-Event") != "workflow_job":
        return {"status": "ignored", "reason": "not a workflow_job event"}

    secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    if not verify_signature(raw_body_bytes, headers.get("X-Hub-Signature-256"), secret):
        return {"status": "rejected", "reason": "invalid signature"}

    event = parse_workflow_job_event(raw_body_bytes)
    runner_request, reason = match_runner_request(
        event,
        required_label=REQUIRED_RUNNER_LABEL,
        org_override=GITHUB_ORG_OVERRIDE,
    )
    if runner_request is None:
        return {"status": "ignored", "reason": reason}

    asyncio.create_task(
        run_github_runner(
            {
                "org": runner_request.org,
                "run_id": runner_request.run_id,
                "labels": runner_request.labels,
                "repository": runner_request.repository,
                "workflow_job_id": runner_request.workflow_job_id,
            }
        )
    )
    return {
        "status": "queued",
        "reason": reason,
        "run_id": runner_request.run_id,
        "labels": runner_request.labels,
    }
