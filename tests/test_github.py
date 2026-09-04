from types import SimpleNamespace

import pytest

import github_runner_orchestrator.github as github


def test_generate_jit_config_registers_runner_at_repository_scope(monkeypatch) -> None:
    calls = []

    def post(url, token, body):
        calls.append((url, token, body))
        return SimpleNamespace(
            status_code=201,
            json=lambda: {"runner": {"id": 17}, "encoded_jit_config": "encoded"},
        )

    monkeypatch.setattr(github, "_post", post)

    config = github.generate_repository_jit_config(
        installation_token="installation-token",
        repository="tensorlakeai/compute-engine-internal",
        runner_name="runner-1",
        runner_group_id=1,
        labels=["self-hosted", "tensorlake"],
        api_base="https://github.example/api/v3",
    )

    assert config.encoded_jit_config == "encoded"
    assert calls == [
        (
            "https://github.example/api/v3/repos/tensorlakeai/compute-engine-internal/"
            "actions/runners/generate-jitconfig",
            "installation-token",
            {
                "name": "runner-1",
                "runner_group_id": 1,
                "labels": ["self-hosted", "tensorlake"],
                "work_folder": "_work",
            },
        )
    ]


@pytest.mark.parametrize(
    "repository",
    ["", "tensorlakeai", "/repo", "owner/", "owner/repo/extra", " owner/repo"],
)
def test_generate_jit_config_rejects_malformed_repository_before_request(
    monkeypatch, repository
) -> None:
    monkeypatch.setattr(
        github,
        "_post",
        lambda *_args, **_kwargs: pytest.fail("malformed repository reached GitHub"),
    )

    with pytest.raises(ValueError, match="exact owner/name pair"):
        github.generate_repository_jit_config(
            installation_token="installation-token",
            repository=repository,
            runner_name="runner-1",
            runner_group_id=1,
            labels=["tensorlake"],
        )
