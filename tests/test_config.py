from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import ConfigError, load_project_config


def test_load_project_config_with_minimal_valid_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: demo
github:
  owner: acme
  repo: widget
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )

    project = load_project_config(project_file)

    assert project.name == "demo"
    assert project.github.owner == "acme"
    assert project.paths.repo_root == (tmp_path / "repo").resolve()


def test_load_project_config_fails_when_required_fields_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: demo
github:
  owner: acme
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_project_config(project_file)

    assert "Invalid project configuration" in str(excinfo.value)