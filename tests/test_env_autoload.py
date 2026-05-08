from __future__ import annotations

from pathlib import Path

import pytest

from cascade import cli as cli_module


@pytest.fixture(autouse=True)
def _host_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_running_in_docker_container", lambda: False)


def test_load_repo_env_defaults_loads_supported_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GH_TOKEN=ghp_from_env_file",
                "OPENROUTER_API_KEY=sk-openrouter-from-env-file",
                "DOCKER_BUILDKIT=1",
                "UNRELATED_VAR=should_not_load",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DOCKER_BUILDKIT", raising=False)
    monkeypatch.delenv("UNRELATED_VAR", raising=False)

    loaded = cli_module.load_repo_env_defaults(repo_root=tmp_path)

    assert loaded["GH_TOKEN"] == "ghp_from_env_file"
    assert loaded["OPENROUTER_API_KEY"] == "sk-openrouter-from-env-file"
    assert loaded["DOCKER_BUILDKIT"] == "1"
    assert "UNRELATED_VAR" not in loaded
    assert cli_module.os.environ["GH_TOKEN"] == "ghp_from_env_file"
    assert cli_module.os.environ["OPENROUTER_API_KEY"] == "sk-openrouter-from-env-file"


def test_load_repo_env_defaults_preserves_existing_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GH_TOKEN=ghp_from_env_file",
                "OPENROUTER_API_KEY=sk-openrouter-from-env-file",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("GH_TOKEN", "ghp_preexisting")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-preexisting")

    loaded = cli_module.load_repo_env_defaults(repo_root=tmp_path)

    assert "GH_TOKEN" not in loaded
    assert "OPENROUTER_API_KEY" not in loaded
    assert cli_module.os.environ["GH_TOKEN"] == "ghp_preexisting"
    assert cli_module.os.environ["OPENROUTER_API_KEY"] == "sk-preexisting"


def test_load_repo_env_defaults_missing_file_is_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    loaded = cli_module.load_repo_env_defaults(repo_root=tmp_path)

    assert loaded == {}
    assert "GITHUB_TOKEN" not in cli_module.os.environ