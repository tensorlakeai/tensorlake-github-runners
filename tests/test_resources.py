import pytest

from github_runner_orchestrator.resources import (
    DEFAULT_RUNNER_RESOURCES,
    MAX_RUNNER_DISK_MB,
    MIN_RUNNER_DISK_MB,
    RUNNER_RESOURCE_PROFILES,
    resources_for_labels,
)


def test_resource_profile_is_selected_from_runner_labels() -> None:
    resources = resources_for_labels(["self-hosted", "tensorlake", "tensorlake-medium"])
    assert resources == RUNNER_RESOURCE_PROFILES["tensorlake-medium"]


def test_default_resources_are_used_without_a_profile_label() -> None:
    assert resources_for_labels(["self-hosted", "tensorlake"]) == DEFAULT_RUNNER_RESOURCES


def test_resource_profiles_use_supported_disk_sizes() -> None:
    assert DEFAULT_RUNNER_RESOURCES.disk_mb == MIN_RUNNER_DISK_MB
    assert RUNNER_RESOURCE_PROFILES["tensorlake-medium"].disk_mb == 51200
    assert RUNNER_RESOURCE_PROFILES["tensorlake-large"].disk_mb == MAX_RUNNER_DISK_MB
    assert RUNNER_RESOURCE_PROFILES["tensorlake-xlarge"].disk_mb == MAX_RUNNER_DISK_MB
    assert all(
        MIN_RUNNER_DISK_MB <= resources.disk_mb <= MAX_RUNNER_DISK_MB
        for resources in RUNNER_RESOURCE_PROFILES.values()
    )


def test_conflicting_resource_profile_labels_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting resource profile labels"):
        resources_for_labels(["tensorlake-small", "tensorlake-large"])
