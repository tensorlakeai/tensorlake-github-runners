from types import SimpleNamespace

import pytest

from github_runner_orchestrator.cache import (
    CACHE_INITIALIZATION_PATH,
    MAX_FILESYSTEM_NAME_LENGTH,
    cache_credential_diagnostics,
    cache_filesystem_name,
    cache_mount_environment,
    ensure_cache_filesystem,
)


def _unsigned_jwt(payload: str) -> str:
    import base64

    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


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
    existing = SimpleNamespace(
        name=cache_filesystem_name("tensorlake/example"),
        status=lambda: SimpleNamespace(version_id="version-1"),
    )

    class Client:
        def __init__(self, *, api_key):
            assert api_key == "project-api-key"

        def list(self):
            return [existing]

        def get(self, name):
            assert name == existing.name
            return existing

        def create(self, _name):
            pytest.fail("existing filesystem should be reused")

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example", "project-api-key") == existing.name


def test_ensure_cache_filesystem_repairs_existing_empty_volume(monkeypatch) -> None:
    writes = []
    existing = SimpleNamespace(
        name=cache_filesystem_name("tensorlake/example"),
        status=lambda: SimpleNamespace(version_id=None),
        write_file=lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    class Client:
        def __init__(self, *, api_key):
            assert api_key == "project-api-key"

        def list(self):
            return [existing]

        def get(self, _name):
            return existing

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example", "project-api-key") == existing.name
    assert writes[0][0][0] == CACHE_INITIALIZATION_PATH


def test_ensure_cache_filesystem_creates_repository_volume(monkeypatch) -> None:
    writes = []
    created = SimpleNamespace(
        name=cache_filesystem_name("tensorlake/example"),
        status=lambda: SimpleNamespace(version_id=None),
        write_file=lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    class Client:
        def __init__(self, *, api_key):
            assert api_key == "project-api-key"

        def list(self):
            return []

        def create(self, _name):
            return created

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example", "project-api-key") == created.name
    assert writes == [
        (
            (
                CACHE_INITIALIZATION_PATH,
                "Initialized for Tensorlake GitHub Actions runner caches.\n",
            ),
            {"message": "Initialize GitHub Actions runner cache"},
        )
    ]


def test_ensure_cache_filesystem_recovers_from_concurrent_creation(monkeypatch) -> None:
    existing = SimpleNamespace(
        name=cache_filesystem_name("tensorlake/example"),
        status=lambda: SimpleNamespace(version_id="version-1"),
    )
    listings = iter([[], [existing]])

    class Client:
        def __init__(self, *, api_key):
            assert api_key == "project-api-key"

        def list(self):
            return next(listings)

        def get(self, name):
            assert name == existing.name
            return existing

        def create(self, _name):
            raise RuntimeError("already exists")

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example", "project-api-key") == existing.name


def test_ensure_cache_filesystem_initialization_race_accepts_other_writer(
    monkeypatch,
) -> None:
    versions = iter([None, "version-1"])

    def write_file(*_args, **_kwargs):
        raise RuntimeError("head changed")

    existing = SimpleNamespace(
        name=cache_filesystem_name("tensorlake/example"),
        status=lambda: SimpleNamespace(version_id=next(versions)),
        write_file=write_file,
    )

    class Client:
        def __init__(self, *, api_key):
            assert api_key == "project-api-key"

        def list(self):
            return [existing]

        def get(self, _name):
            return existing

    monkeypatch.setattr("tensorlake.filesystem.FilesystemClient", Client)

    assert ensure_cache_filesystem("tensorlake/example", "project-api-key") == existing.name


def test_cache_mount_environment_uses_filesystem_scoped_credential(monkeypatch) -> None:
    credential = SimpleNamespace(token="scoped-token", git_username="scoped-user")

    class Client:
        project_id = "project_example"

        def __init__(self, *, api_key):
            assert api_key == "project-api-key"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def credential(self, name):
            assert name == "github-actions-cache-example"
            return credential

    monkeypatch.setattr("tensorlake.repositories.RepositoryClient", Client)
    monkeypatch.setenv("TENSORLAKE_API_URL", "https://api.example.test")

    assert cache_mount_environment("github-actions-cache-example", "project-api-key") == {
        "TENSORLAKE_GIT_TOKEN": "scoped-token",
        "TENSORLAKE_GIT_USERNAME": "scoped-user",
        "TENSORLAKE_PROJECT_ID": "project_example",
        "TENSORLAKE_API_URL": "https://api.example.test",
    }


def test_cache_credential_diagnostics_are_log_safe_and_report_expiry(monkeypatch) -> None:
    monkeypatch.setattr("github_runner_orchestrator.cache.time.time", lambda: 1_700_000_000)
    token = _unsigned_jwt('{"sub":"api-key:secret-id","exp":1700003600}')

    diagnostics = cache_credential_diagnostics(token)

    assert diagnostics == {
        "credential_fingerprint": "0b663af87714",
        "credential_expires_at_unix": 1_700_003_600,
        "credential_remaining_secs": 3_600,
    }
    assert token not in str(diagnostics)
    assert "secret-id" not in str(diagnostics)


def test_cache_credential_diagnostics_tolerate_opaque_tokens() -> None:
    diagnostics = cache_credential_diagnostics("opaque-development-token")

    assert diagnostics["credential_expires_at_unix"] is None
    assert diagnostics["credential_remaining_secs"] is None
    assert diagnostics["credential_fingerprint"]
