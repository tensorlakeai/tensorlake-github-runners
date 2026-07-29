import asyncio
import os

from tensorlake.applications import HttpBody, Image, RequestContext, application, function

from github_runner_orchestrator.cache import (
    CACHE_MOUNT_PATH,
    CACHE_SETTLE_SECONDS,
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

REQUIRED_RUNNER_LABEL = "tensorlake"
RUNNER_IMAGE = "github-actions-runner"
RUNNER_TIMEOUT_SECS = 7200
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
        docker_info = await sandbox.run("docker", ["info"], timeout=10)
        if docker_info.exit_code == 0:
            return
        await asyncio.sleep(1)

    raise RuntimeError("Docker's systemd service did not become ready in the sandbox")


async def _settle_cache_writes(sandbox) -> None:
    sync_result = await sandbox.run("sync", [], timeout=30)
    if sync_result.exit_code != 0:
        print(f"Warning: cache sync failed: {sync_result.stderr or sync_result.stdout}")
    await asyncio.sleep(CACHE_SETTLE_SECONDS)


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
    from tensorlake.sandbox import AsyncSandbox, FileSystemMount

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

    cache_filesystem_id = None
    if request.repository:
        try:
            cache_filesystem_id = await asyncio.to_thread(
                ensure_cache_filesystem,
                request.repository,
            )
        except Exception as error:
            print(
                f"Warning: running {request.repository} without persistent cache: "
                f"{type(error).__name__}: {error}"
            )

    file_systems = None
    if cache_filesystem_id:
        file_systems = [
            FileSystemMount(
                file_system_id=cache_filesystem_id,
                mount_path=CACHE_MOUNT_PATH,
            )
        ]

    sandbox_create_options = {
        "image": RUNNER_IMAGE,
        "cpus": resources.cpus,
        "memory_mb": resources.memory_mb,
        "disk_mb": resources.disk_mb,
        "timeout_secs": RUNNER_TIMEOUT_SECS,
    }
    try:
        sandbox = await AsyncSandbox.create(
            **sandbox_create_options,
            file_systems=file_systems,
        )
    except Exception as error:
        if not cache_filesystem_id:
            raise
        print(
            f"Warning: cache mount failed; creating runner without persistent cache: "
            f"{type(error).__name__}: {error}"
        )
        cache_filesystem_id = None
        sandbox = await AsyncSandbox.create(**sandbox_create_options)

    try:
        await _wait_for_docker(sandbox)

        runner_environment = {"RUNNER_ALLOW_RUNASROOT": "1"}
        if cache_filesystem_id:
            runner_environment["TENSORLAKE_CACHE_DIR"] = CACHE_MOUNT_PATH

        result = await sandbox.run(
            "/opt/actions-runner/run.sh",
            ["--jitconfig", jit.encoded_jit_config],
            env=runner_environment,
            working_dir="/opt/actions-runner",
        )
        return {
            "sandbox_id": sandbox.sandbox_id,
            "runner_name": runner_name,
            "exit_code": int(result.exit_code or 0),
            "cache_filesystem_id": cache_filesystem_id,
            "stdout_tail": (result.stdout or "")[-4000:],
            "stderr_tail": (result.stderr or "")[-4000:],
        }
    finally:
        if cache_filesystem_id:
            try:
                await _settle_cache_writes(sandbox)
            except Exception as error:
                print(f"Warning: failed to settle cache writes: {type(error).__name__}: {error}")
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
