"""Tensorlake deployment entrypoint.

Keeping this file at the repository root makes the complete
``github_runner_orchestrator`` package part of the deployed source tree.
"""

from github_runner_orchestrator.app import github_runner_webhook, run_github_runner

__all__ = ["github_runner_webhook", "run_github_runner"]
