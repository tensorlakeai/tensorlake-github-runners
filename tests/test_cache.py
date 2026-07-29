from types import SimpleNamespace

import pytest

from github_runner_orchestrator.cache import (
    MAX_FILESYSTEM_NAME_LENGTH,
    cache_mount_environment,
    cache_filesystem_name,
    ensure_cache_filesystem,
)


def test_cache_filesystem_name_is_stable_distinct_and_bounded() -> None:
    first = cache_filesystem_name("example-org/large-rust-repository")
    assert first == cache_filesystem_name("example-org/large-rust-repository")
    assert first != cache_filesystem_name("example-org/another-repository")
    assert first.startswith("github-actions-cache-example-org-large-rust")
    assert len(first) <= MAX_FILESYSTEM_NAME_LENGTH


def test_cache_filesystem_name_rejects_missing_repository() -> None:
    with pytest.raises(ValueError, match="repository is required"):
        cache_filesystem_name("")


def test_ensure_cache_filesystem_reuses_existing_project_volume(monkeypatch) -> None:
    existing = SimpleNamespace(name=cache_filesystem_name("tensorlake/example"))

    class Client:
        def list(self):
            return [existing]

        def create(self, _name):
            pytest.fail("existing filesystem should be reused")

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example") == existing.name


def test_ensure_cache_filesystem_creates_repository_volume(monkeypatch) -> None:
    created = SimpleNamespace(name=cache_filesystem_name("tensorlake/example"))

    class Client:
        def list(self):
            return []

        def create(self, _name):
            return created

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example") == created.name


def test_ensure_cache_filesystem_recovers_from_concurrent_creation(monkeypatch) -> None:
    existing = SimpleNamespace(name=cache_filesystem_name("tensorlake/example"))
    listings = iter([[], [existing]])

    class Client:
        def list(self):
            return next(listings)

        def create(self, _name):
            raise RuntimeError("already exists")

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example") == existing.name


def test_cache_mount_environment_uses_filesystem_scoped_credential(monkeypatch) -> None:
    credential = SimpleNamespace(token="scoped-token", git_username="scoped-user")

    class Client:
        project_id = "project_example"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def credential(self, name):
            assert name == "github-actions-cache-example"
            return credential

    monkeypatch.setattr(
        "tensorlake.repositories.RepositoryClient.from_env",
        lambda: Client(),
    )
    monkeypatch.setenv("TENSORLAKE_API_URL", "https://api.example.test")

    assert cache_mount_environment("github-actions-cache-example") == {
        "TENSORLAKE_GIT_TOKEN": "scoped-token",
        "TENSORLAKE_GIT_USERNAME": "scoped-user",
        "TENSORLAKE_PROJECT_ID": "project_example",
        "TENSORLAKE_API_URL": "https://api.example.test",
    }
