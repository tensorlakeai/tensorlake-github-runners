from __future__ import annotations

from github_runner_orchestrator.models import RunnerResources


DEFAULT_RUNNER_RESOURCES = RunnerResources(cpus=2, memory_mb=4096, disk_mb=51200)

# Edit these profiles to match the runner sizes you want to expose to workflows.
RUNNER_RESOURCE_PROFILES: dict[str, RunnerResources] = {
    "tensorlake-small": DEFAULT_RUNNER_RESOURCES,
    "tensorlake-medium": RunnerResources(cpus=4, memory_mb=8192, disk_mb=102400),
    "tensorlake-large": RunnerResources(cpus=8, memory_mb=16384, disk_mb=204800),
}


def resources_for_labels(labels: list[str]) -> RunnerResources:
    matched_profiles = [label for label in labels if label in RUNNER_RESOURCE_PROFILES]
    if len(matched_profiles) > 1:
        raise ValueError(
            "runner request contains conflicting resource profile labels: "
            + ", ".join(matched_profiles)
        )
    if not matched_profiles:
        return DEFAULT_RUNNER_RESOURCES
    return RUNNER_RESOURCE_PROFILES[matched_profiles[0]]
