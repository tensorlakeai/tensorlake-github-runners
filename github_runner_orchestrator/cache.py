from __future__ import annotations

import hashlib
import os
import re

CACHE_FILESYSTEM_NAME_PREFIX = "github-actions-cache"
CACHE_MOUNT_PATH = "/mnt/tensorlake-cache"
CACHE_SETTLE_SECONDS = 6
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
    """Find or lazily create the Cloud Volume used by one repository."""
    from tensorlake.filesystem import FilesystemClient

    name = cache_filesystem_name(repository)
    client = FilesystemClient()

    def existing_name() -> str | None:
        for file_system in client.list():
            if file_system.name == name:
                return file_system.name
        return None

    file_system_name = existing_name()
    if file_system_name:
        return file_system_name

    try:
        created = client.create(name)
    except Exception:
        # Concurrent first jobs can race to create the same named Cloud Volume.
        # Re-read the project before surfacing the creation error.
        file_system_name = existing_name()
        if file_system_name:
            return file_system_name
        raise

    return created.name


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
