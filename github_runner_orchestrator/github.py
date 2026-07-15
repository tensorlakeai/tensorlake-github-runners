from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import requests

from github_runner_orchestrator.models import AppCredentials, JitConfig

GITHUB_API_VERSION = "2022-11-28"
DEFAULT_API_BASE = "https://api.github.com"


def mint_app_jwt(credentials: AppCredentials, now: int | None = None) -> str:
    issued_at = int(now or time.time()) - 60
    payload = {
        "iat": issued_at,
        "exp": issued_at + 600,
        "iss": credentials.app_client_id,
    }
    return jwt.encode(payload, credentials.private_key, algorithm="RS256")


def _post(url: str, token: str, body: dict[str, Any] | None = None) -> requests.Response:
    return requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        json=body,
        timeout=30,
    )


def get_installation_token(
    credentials: AppCredentials,
    api_base: str = DEFAULT_API_BASE,
) -> str:
    app_jwt = mint_app_jwt(credentials)
    response = _post(
        f"{api_base}/app/installations/{credentials.installation_id}/access_tokens",
        app_jwt,
    )
    if response.status_code != 201:
        raise RuntimeError(
            f"installation token request failed: {response.status_code} {response.text}"
        )
    return str(response.json()["token"])


def generate_org_jit_config(
    installation_token: str,
    org: str,
    runner_name: str,
    runner_group_id: int,
    labels: list[str],
    api_base: str = DEFAULT_API_BASE,
    work_folder: str = "_work",
) -> JitConfig:
    response = _post(
        f"{api_base}/orgs/{org}/actions/runners/generate-jitconfig",
        installation_token,
        {
            "name": runner_name,
            "runner_group_id": runner_group_id,
            "labels": labels,
            "work_folder": work_folder,
        },
    )
    if response.status_code != 201:
        raise RuntimeError(f"generate-jitconfig failed: {response.status_code} {response.text}")
    data = response.json()
    return JitConfig(runner=data["runner"], encoded_jit_config=data["encoded_jit_config"])


def build_runner_name(run_id: int | None) -> str:
    return f"tl-gh-runner-{run_id or 'unknown'}-{uuid.uuid4().hex[:8]}"
