from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time

CACHE_FILESYSTEM_NAME_PREFIX = "github-actions-cache"
CACHE_INITIALIZATION_PATH = ".tensorlake-cache-initialized"
CACHE_MOUNT_PATH = "/mnt/tensorlake-cache"
CACHE_UNMOUNT_MAX_ATTEMPTS = 15
CACHE_UNMOUNT_RETRY_SECONDS = 1
MAX_FILESYSTEM_NAME_LENGTH = 63


def cache_filesystem_name(repository: str) -> str:
    """Return a deterministic, project-unique cache filesystem name."""
    if not repository:
        raise ValueError("repository is required to create a cache filesystem")

    slug = re.sub(r"[^a-z0-9]+", "-", repository.lower()).strip("-") or "repository"
    digest = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:12]
    suffix = f"-{digest}"
    available_slug_length = (
        MAX_FILESYSTEM_NAME_LENGTH - len(CACHE_FILESYSTEM_NAME_PREFIX) - len(suffix) - 1
    )
    return f"{CACHE_FILESYSTEM_NAME_PREFIX}-{slug[:available_slug_length]}{suffix}"


def ensure_cache_filesystem(repository: str) -> str:
    """Find, create, and initialize the Cloud Volume used by one repository."""
    from tensorlake.filesystem import FilesystemClient

    name = cache_filesystem_name(repository)
    client = FilesystemClient()

    def existing_filesystem():
        for file_system in client.list():
            if file_system.name == name:
                return client.get(name)
        return None

    file_system = existing_filesystem()

    if file_system is None:
        try:
            file_system = client.create(name)
        except Exception:
            # Concurrent first jobs can race to create the same named Cloud Volume.
            # Re-read the project before surfacing the creation error.
            file_system = existing_filesystem()
            if file_system is None:
                raise

    _initialize_cache_filesystem(file_system)
    return file_system.name


def _initialize_cache_filesystem(file_system) -> None:
    """Give an empty filesystem the initial generation required by mounts."""
    if file_system.status().version_id is not None:
        return

    try:
        file_system.write_file(
            CACHE_INITIALIZATION_PATH,
            "Initialized for Tensorlake GitHub Actions runner caches.\n",
            message="Initialize GitHub Actions runner cache",
        )
    except Exception:
        # Concurrent first jobs can both observe an empty filesystem. If the
        # other job initialized it, the desired postcondition already holds.
        if file_system.status().version_id is not None:
            return
        raise


def cache_mount_environment(file_system_name: str) -> dict[str, str]:
    """Mint the filesystem-scoped credential consumed by ``tl fs mount``."""
    from tensorlake.repositories import RepositoryClient

    with RepositoryClient.from_env() as client:
        credential = client.credential(file_system_name)
        environment = {
            "TENSORLAKE_GIT_TOKEN": credential.token,
            "TENSORLAKE_GIT_USERNAME": credential.git_username,
            "TENSORLAKE_PROJECT_ID": client.project_id,
        }

    if api_url := os.environ.get("TENSORLAKE_API_URL"):
        environment["TENSORLAKE_API_URL"] = api_url
    return environment


def cache_credential_diagnostics(token: str) -> dict[str, str | int | None]:
    """Return log-safe identity and lifetime fields for a mount credential."""
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    expires_at: int | None = None
    parts = token.split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            claim_expiry = claims.get("exp")
            if isinstance(claim_expiry, int):
                expires_at = claim_expiry
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    return {
        "credential_fingerprint": fingerprint,
        "credential_expires_at_unix": expires_at,
        "credential_remaining_secs": (
            max(0, expires_at - int(time.time())) if expires_at is not None else None
        ),
    }
