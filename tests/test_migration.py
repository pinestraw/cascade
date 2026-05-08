from __future__ import annotations

from pathlib import Path

from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig
from cascade.migration import detect_docker_era_state, migrate_docker_era_state


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(
            workspace_root=tmp_path,
            repo_root=tmp_path / "jungle",
            worktree_root=tmp_path / "jungle-worktrees",
            secrets_root=tmp_path / "jungle-secrets",
        ),
        commands=CommandsConfig(create_worktree="echo create"),
    )


def test_detect_docker_era_state_keys() -> None:
    state = {
        "project_file": "/workspace/cascade/examples/jungle.yaml",
        "worktree": "/workspace/jungle-worktrees/a1-test",
        "run_dir": "/workspace/cascade/state/jungle/runs/a1",
    }
    stale = detect_docker_era_state(state)
    assert set(stale) == {"project_file", "worktree", "run_dir"}


def test_migrate_docker_era_state_remaps_paths(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    state = {
        "agent": "a1",
        "project_file": "/workspace/cascade/examples/jungle.yaml",
        "worktree": "/workspace/jungle-worktrees/a1-test",
        "run_dir": "/workspace/cascade/state/jungle/runs/a1",
        "repo_root": "/workspace/jungle",
        "secrets_root": "/workspace/jungle-secrets",
    }

    migrated, changes = migrate_docker_era_state(state, project_config=proj)

    assert migrated["project_file"] == str((Path.cwd() / "examples" / "jungle.yaml").resolve())
    assert migrated["worktree"] == str((proj.paths.worktree_root / "a1-test").resolve())
    assert migrated["run_dir"] == str((Path.cwd() / "state" / "jungle" / "runs" / "a1").resolve())
    assert migrated["repo_root"] == str(proj.paths.repo_root.resolve())
    assert migrated["secrets_root"] == str(proj.paths.secrets_root.resolve())
    assert changes
