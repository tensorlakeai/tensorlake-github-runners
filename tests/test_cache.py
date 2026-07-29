from types import SimpleNamespace

import pytest

from github_runner_orchestrator.cache import (
    MAX_FILESYSTEM_NAME_LENGTH,
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
    existing = SimpleNamespace(name=cache_filesystem_name("tensorlake/example"), id="file_system_1")
    monkeypatch.setattr("tensorlake.sandbox.list_file_systems", lambda: [existing])
    monkeypatch.setattr(
        "tensorlake.sandbox.create_file_system",
        lambda *_args, **_kwargs: pytest.fail("existing filesystem should be reused"),
    )

    assert ensure_cache_filesystem("tensorlake/example") == "file_system_1"


def test_ensure_cache_filesystem_creates_repository_volume(monkeypatch) -> None:
    created = SimpleNamespace(id="file_system_2")
    monkeypatch.setattr("tensorlake.sandbox.list_file_systems", lambda: [])
    monkeypatch.setattr("tensorlake.sandbox.create_file_system", lambda *_args, **_kwargs: created)

    assert ensure_cache_filesystem("tensorlake/example") == "file_system_2"


def test_ensure_cache_filesystem_recovers_from_concurrent_creation(monkeypatch) -> None:
    existing = SimpleNamespace(name=cache_filesystem_name("tensorlake/example"), id="file_system_3")
    listings = iter([[], [existing]])
    monkeypatch.setattr("tensorlake.sandbox.list_file_systems", lambda: next(listings))

    def race(*_args, **_kwargs):
        raise RuntimeError("already exists")

    monkeypatch.setattr("tensorlake.sandbox.create_file_system", race)

    assert ensure_cache_filesystem("tensorlake/example") == "file_system_3"
