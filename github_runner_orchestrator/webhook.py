from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any

from github_runner_orchestrator.models import RunnerRequest


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_workflow_job_event(raw_body: bytes) -> dict[str, Any]:
    return json.loads(raw_body.decode("utf-8"))


def match_runner_request(
    event: dict[str, Any],
    required_label: str,
    org_override: str | None = None,
) -> tuple[RunnerRequest | None, str]:
    if event.get("action") != "queued":
        return None, f"action {event.get('action')!r} is not 'queued'"

    workflow_job = event.get("workflow_job") or {}
    labels = list(workflow_job.get("labels") or [])
    if required_label not in labels:
        return None, f"labels {labels!r} do not include required label {required_label!r}"

    org = org_override or (event.get("organization") or {}).get("login")
    if not org:
        return None, "organization.login is missing"

    repository = (event.get("repository") or {}).get("full_name")
    if not isinstance(repository, str) or repository.count("/") != 1:
        return None, "repository.full_name is missing or malformed"
    repository_owner, repository_name = repository.split("/", 1)
    if not repository_owner or not repository_name:
        return None, "repository.full_name is missing or malformed"
    if repository_owner.casefold() != org.casefold():
        return None, "repository owner does not match organization"
    return (
        RunnerRequest(
            org=org,
            run_id=workflow_job.get("run_id"),
            labels=labels,
            repository=repository,
            workflow_job_id=workflow_job.get("id"),
        ),
        f"queued job requests {required_label!r}",
    )
