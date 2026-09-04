from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunnerRequest:
    org: str
    run_id: int | None
    labels: list[str]
    repository: str
    workflow_job_id: int | None = None


@dataclass(frozen=True)
class RunnerResources:
    cpus: float
    memory_mb: int
    disk_mb: int


@dataclass(frozen=True)
class AppCredentials:
    app_client_id: str
    installation_id: str
    private_key: str


@dataclass(frozen=True)
class JitConfig:
    runner: dict[str, Any]
    encoded_jit_config: str


@dataclass(frozen=True)
class SandboxRunnerResult:
    sandbox_id: str
    runner_name: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str
