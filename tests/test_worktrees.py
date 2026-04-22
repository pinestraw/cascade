from __future__ import annotations

from pathlib import Path

from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig
from cascade.worktrees import find_worktree_path, resolve_worktree_path, slugify


def make_project(tmp_path: Path) -> ProjectConfig:
    repo_root = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    repo_root.mkdir()
    worktree_root.mkdir()
    return ProjectConfig(
        name="demo",
        github=GithubConfig(owner="owner", repo="repo"),
        paths=PathsConfig(repo_root=repo_root, worktree_root=worktree_root),
        commands=CommandsConfig(create_worktree="echo hi"),
    )


def test_slugify_expected_examples() -> None:
    assert slugify("Daily Digest Email — Implementation Plan #45") == "daily-digest-email-implementation-plan-45"
    assert slugify("API performance realistic Pass Suite") == "api-performance-realistic-pass-suite"


def test_slugify_collapses_weird_punctuation() -> None:
    assert slugify("Hello!!! --- ??? World") == "hello-world"


def test_slugify_applies_max_length() -> None:
    source = "a" * 120
    assert slugify(source, max_length=80) == "a" * 80


def test_resolve_worktree_path_uses_convention(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    assert resolve_worktree_path(project, "oc1", "my-slug") == project.paths.worktree_root / "oc1-my-slug"


def test_find_worktree_path_uses_existing_candidate(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    expected = project.paths.worktree_root / "oc1_my-slug"
    expected.mkdir()

    found, warning = find_worktree_path(project, "oc1", "my-slug")

    assert found == expected.resolve()
    assert warning is None


def test_find_worktree_path_warns_when_none_exist(tmp_path: Path) -> None:
    project = make_project(tmp_path)

    found, warning = find_worktree_path(project, "oc1", "my-slug")

    assert found == project.paths.worktree_root / "oc1-my-slug"
    assert warning is not None


def test_find_worktree_path_warns_when_multiple_exist(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project.paths.worktree_root / "oc1-my-slug").mkdir()
    (project.paths.worktree_root / "oc1_my-slug").mkdir()

    found, warning = find_worktree_path(project, "oc1", "my-slug")

    assert found == project.paths.worktree_root / "oc1-my-slug"
    assert warning is not None