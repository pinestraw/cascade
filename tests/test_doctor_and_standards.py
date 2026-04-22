from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig
from cascade.doctor import has_failures, run_doctor_checks
from cascade.standards import validate_worktree_location


def test_doctor_missing_opencode_is_warning_not_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return None
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())

    checks = run_doctor_checks(project_file)
    opencode_check = [check for check in checks if check.name == "OpenCode CLI"][0]

    assert opencode_check.status == "warn"
    assert not has_failures(checks)


def test_validate_worktree_location_outside_root_rejected(tmp_path: Path) -> None:
    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "worktrees"),
        commands=CommandsConfig(create_worktree="echo create"),
    )
    outside = tmp_path / "other" / "agent-worktree"

    is_valid, message = validate_worktree_location(project, outside)

    assert not is_valid
    assert "outside configured worktree_root" in message
