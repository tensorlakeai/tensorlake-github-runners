from __future__ import annotations

import hashlib
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
    """Find or lazily create the project filesystem used by one repository."""
    from tensorlake.sandbox import create_file_system, list_file_systems

    name = cache_filesystem_name(repository)

    def existing_id() -> str | None:
        for file_system in list_file_systems():
            if file_system.name == name and file_system.id:
                return file_system.id
        return None

    file_system_id = existing_id()
    if file_system_id:
        return file_system_id

    try:
        created = create_file_system(
            name,
            description=f"Persistent GitHub Actions cache for {repository}",
        )
    except Exception:
        # Concurrent first jobs can race to create the same named filesystem.
        # Re-read the project before surfacing the creation error.
        file_system_id = existing_id()
        if file_system_id:
            return file_system_id
        raise

    if not created.id:
        raise RuntimeError(f"Tensorlake created cache filesystem {name!r} without an id")
    return created.id
