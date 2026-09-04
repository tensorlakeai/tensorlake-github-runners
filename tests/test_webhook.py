import hmac
import json
from hashlib import sha256

from github_runner_orchestrator.webhook import match_runner_request, verify_signature


def _signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def test_verify_signature_accepts_valid_header() -> None:
    body = b'{"zen":"Keep it logically awesome."}'
    assert verify_signature(body, _signature(body, "secret"), "secret")


def test_verify_signature_rejects_invalid_header() -> None:
    assert not verify_signature(b"{}", "sha256=bad", "secret")


def test_match_runner_request_accepts_queued_required_label() -> None:
    event = {
        "action": "queued",
        "organization": {"login": "tensorlake"},
        "repository": {"full_name": "tensorlake/example"},
        "workflow_job": {"id": 10, "run_id": 20, "labels": ["self-hosted", "tensorlake"]},
    }
    request, reason = match_runner_request(event, "tensorlake")
    assert request is not None
    assert request.org == "tensorlake"
    assert request.repository == "tensorlake/example"
    assert request.run_id == 20
    assert reason == "queued job requests 'tensorlake'"


def test_match_runner_request_ignores_missing_label() -> None:
    event = json.loads(
        """
        {
          "action": "queued",
          "organization": {"login": "tensorlake"},
          "workflow_job": {"labels": ["self-hosted"]}
        }
        """
    )
    request, reason = match_runner_request(event, "tensorlake")
    assert request is None
    assert "do not include" in reason


def test_match_runner_request_rejects_missing_repository() -> None:
    event = {
        "action": "queued",
        "organization": {"login": "tensorlake"},
        "workflow_job": {"labels": ["self-hosted", "tensorlake"]},
    }

    request, reason = match_runner_request(event, "tensorlake")

    assert request is None
    assert reason == "repository.full_name is missing or malformed"


def test_match_runner_request_rejects_repository_from_another_organization() -> None:
    event = {
        "action": "queued",
        "organization": {"login": "tensorlake"},
        "repository": {"full_name": "other-org/example"},
        "workflow_job": {"labels": ["self-hosted", "tensorlake"]},
    }

    request, reason = match_runner_request(event, "tensorlake")

    assert request is None
    assert reason == "repository owner does not match organization"
