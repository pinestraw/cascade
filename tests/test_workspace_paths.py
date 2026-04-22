"""Tests for workspace-boundary path resolution and validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import (
    CommandsConfig,
    ConfigError,
    GithubConfig,
    PathsConfig,
    ProjectConfig,
    is_inside_workspace,
    load_project_config,
    resolve_workspace_root,
    validate_project_paths,
    workspace_root_is_broad,
)
from cascade.doctor import has_failures, run_doctor_checks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workspace_yaml(
    project_file: Path,
    workspace_root: str = "..",
    repo_root: str = "jungle",
    worktree_root: str = "jungle-worktrees",
    secrets_root: str | None = "jungle-secrets",
    related_repos: str = "",
) -> None:
    secrets_line = f"  secrets_root: {secrets_root!r}\n" if secrets_root is not None else ""
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: {workspace_root!r}
  repo_root: {repo_root!r}
  worktree_root: {worktree_root!r}
{secrets_line}
{related_repos}
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. New-style config resolves paths relative to workspace_root
# ---------------------------------------------------------------------------


def test_new_style_config_resolves_paths_relative_to_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "instica-workspace"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()
    (workspace / "jungle-secrets").mkdir()
    (workspace / "jungle-infrastructure").mkdir()

    monkeypatch.chdir(cascade_dir)

    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(
        project_file,
        workspace_root="..",
        repo_root="jungle",
        worktree_root="jungle-worktrees",
        secrets_root="jungle-secrets",
        related_repos="related_repos:\n  infrastructure: jungle-infrastructure",
    )

    project = load_project_config(project_file)

    assert project.paths.workspace_root == workspace.resolve()
    assert project.paths.repo_root == (workspace / "jungle").resolve()
    assert project.paths.worktree_root == (workspace / "jungle-worktrees").resolve()
    assert project.paths.secrets_root == (workspace / "jungle-secrets").resolve()
    assert project.related_repos["infrastructure"] == (workspace / "jungle-infrastructure").resolve()


def test_new_style_config_workspace_root_stored_on_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(project_file, workspace_root="..", secrets_root=None)

    project = load_project_config(project_file)
    ws = resolve_workspace_root(project)

    assert ws is not None
    assert ws == workspace.resolve()


# ---------------------------------------------------------------------------
# 2. Path escape rejected
# ---------------------------------------------------------------------------


def test_path_escape_outside_workspace_fails_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    # No jungle dir — repo_root points outside workspace
    outside = tmp_path / "outside-repo"
    outside.mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    project_file.write_text(
        """
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: ".."
  repo_root: "../outside-repo"
  worktree_root: "jungle-worktrees"
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )

    project = load_project_config(project_file)
    results = validate_project_paths(project)
    repo_result = next(r for r in results if r.key == "repo_root")

    assert repo_result.status == "fail"
    assert "outside workspace_root" in repo_result.message


def test_is_inside_workspace_true_for_child(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    child = ws / "jungle"
    child.mkdir()
    assert is_inside_workspace(child, ws) is True


def test_is_inside_workspace_false_for_sibling(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    sibling = tmp_path / "other"
    sibling.mkdir()
    assert is_inside_workspace(sibling, ws) is False


def test_is_inside_workspace_true_for_workspace_itself(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert is_inside_workspace(ws, ws) is True


# ---------------------------------------------------------------------------
# 3. Related repos validation
# ---------------------------------------------------------------------------


def test_related_repo_inside_workspace_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()
    (workspace / "jungle-infrastructure").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(
        project_file,
        secrets_root=None,
        related_repos="related_repos:\n  infrastructure: jungle-infrastructure",
    )

    project = load_project_config(project_file)
    results = validate_project_paths(project)
    infra_result = next(r for r in results if r.key == "related_repos.infrastructure")

    assert infra_result.status == "ok"


def test_missing_related_repo_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()
    # jungle-infrastructure intentionally absent

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(
        project_file,
        secrets_root=None,
        related_repos="related_repos:\n  infrastructure: jungle-infrastructure",
    )

    project = load_project_config(project_file)
    results = validate_project_paths(project)
    infra_result = next(r for r in results if r.key == "related_repos.infrastructure")

    assert infra_result.status == "warn"


def test_related_repo_outside_workspace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    project_file.write_text(
        """
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: ".."
  repo_root: "jungle"
  worktree_root: "jungle-worktrees"
related_repos:
  outside: "../../totally-external"
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )

    project = load_project_config(project_file)
    results = validate_project_paths(project)
    outside_result = next(r for r in results if r.key == "related_repos.outside")

    assert outside_result.status == "fail"
    assert "outside workspace_root" in outside_result.message


# ---------------------------------------------------------------------------
# 4. Broad workspace warning
# ---------------------------------------------------------------------------


def test_workspace_root_named_github_projects_is_broad() -> None:
    assert workspace_root_is_broad(Path("/Users/alice/github-projects")) is True


def test_workspace_root_named_documents_is_broad() -> None:
    assert workspace_root_is_broad(Path("/Users/alice/Documents")) is True


def test_workspace_root_named_instica_workspace_is_not_broad() -> None:
    assert workspace_root_is_broad(Path("/Users/alice/instica-workspace")) is False


def test_workspace_root_named_myproject_is_not_broad() -> None:
    assert workspace_root_is_broad(Path("/Users/alice/my-project-workspace")) is False


def test_broad_workspace_produces_warn_not_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate workspace named "documents" (broad heuristic)
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    cascade_dir = documents_dir / "cascade"
    cascade_dir.mkdir()
    (documents_dir / "jungle").mkdir()
    (documents_dir / "jungle-worktrees").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(project_file, workspace_root="..", secrets_root=None)

    project = load_project_config(project_file)
    results = validate_project_paths(project)
    broad = next((r for r in results if r.key == "workspace_root_broad"), None)

    assert broad is not None
    assert broad.status == "warn"
    # Should not produce a fail — just warn
    fail_results = [r for r in results if r.status == "fail"]
    assert not fail_results


# ---------------------------------------------------------------------------
# 5. Doctor includes workspace_root / repo_root / worktree_root checks
# ---------------------------------------------------------------------------


def _stub_which_gh_only(name: str) -> str | None:
    if name == "gh":
        return "/usr/bin/gh"
    return None


class _GhAuthOk:
    returncode = 0
    stdout = "authenticated"


def test_doctor_reports_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(project_file, secrets_root=None)

    monkeypatch.setattr("cascade.doctor.shutil.which", _stub_which_gh_only)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *a, **k: _GhAuthOk())

    checks = run_doctor_checks(project_file)
    check_names = {c.name for c in checks}

    assert "workspace_root" in check_names
    assert "repo_root" in check_names
    assert "worktree_root" in check_names


def test_doctor_workspace_root_ok_when_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(project_file, secrets_root=None)

    monkeypatch.setattr("cascade.doctor.shutil.which", _stub_which_gh_only)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *a, **k: _GhAuthOk())

    checks = run_doctor_checks(project_file)
    ws_check = next(c for c in checks if c.name == "workspace_root")

    assert ws_check.status == "ok"
    assert not has_failures(checks)


def test_doctor_reports_related_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cascade_dir = workspace / "cascade"
    cascade_dir.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()
    (workspace / "jungle-infrastructure").mkdir()

    monkeypatch.chdir(cascade_dir)
    project_file = cascade_dir / "project.yaml"
    _write_workspace_yaml(
        project_file,
        secrets_root=None,
        related_repos="related_repos:\n  infrastructure: jungle-infrastructure",
    )

    monkeypatch.setattr("cascade.doctor.shutil.which", _stub_which_gh_only)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *a, **k: _GhAuthOk())

    checks = run_doctor_checks(project_file)
    check_names = {c.name for c in checks}

    assert "related_repos.infrastructure" in check_names


# ---------------------------------------------------------------------------
# 6. Context pack includes allowed paths and excludes undeclared sibling repos
# ---------------------------------------------------------------------------


def test_context_pack_operating_rules_includes_workspace_boundary(tmp_path: Path) -> None:
    from cascade.context_pack import _operating_rules_block

    rules = _operating_rules_block(
        workspace_root="/workspace",
        allowed_paths=["repo_root: /workspace/jungle", "worktree_root: /workspace/jungle-worktrees"],
    )

    assert "Only operate inside the assigned worktree and explicitly declared project paths." in rules
    assert "Do not inspect or edit unrelated sibling repositories in the workspace." in rules
    assert "/workspace" in rules
    assert "repo_root: /workspace/jungle" in rules


def test_context_pack_operating_rules_without_workspace_still_safe(tmp_path: Path) -> None:
    from cascade.context_pack import _operating_rules_block

    rules = _operating_rules_block()

    assert "Only operate inside the assigned worktree and explicitly declared project paths." in rules
    assert "Do not inspect or edit unrelated sibling repositories in the workspace." in rules


# ---------------------------------------------------------------------------
# 7. Prompt includes workspace boundary and sibling-repo warning
# ---------------------------------------------------------------------------


def test_launch_prompt_includes_workspace_boundary_when_configured(tmp_path: Path) -> None:
    from cascade.prompts import build_launch_prompt

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "jungle").mkdir()
    (workspace / "jungle-worktrees").mkdir()

    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(
            workspace_root=workspace,
            repo_root=workspace / "jungle",
            worktree_root=workspace / "jungle-worktrees",
        ),
        commands=CommandsConfig(create_worktree="echo create"),
    )
    agent_state = {
        "agent": "oc1",
        "issue": 10,
        "title": "Test issue",
        "worktree": str(workspace / "jungle-worktrees" / "oc1-test"),
    }

    prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body="Do the work.",
        instruction_files=[],
    )

    assert "workspace_root" in prompt
    assert str(workspace) in prompt
    assert "Only operate inside the assigned worktree and explicitly declared project paths." in prompt
    assert "Do not inspect or edit unrelated sibling repositories in the workspace." in prompt


def test_launch_prompt_workspace_boundary_absent_when_not_configured(tmp_path: Path) -> None:
    from cascade.prompts import build_launch_prompt

    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(
            repo_root=tmp_path / "jungle",
            worktree_root=tmp_path / "jungle-worktrees",
        ),
        commands=CommandsConfig(create_worktree="echo create"),
    )
    agent_state = {"agent": "oc1", "issue": 1, "title": "T", "worktree": "/tmp/wt"}

    prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body="Do it.",
        instruction_files=[],
    )

    # workspace_root section should not appear when not configured
    assert "workspace_root:" not in prompt


def test_launch_prompt_sibling_repo_warning_always_present(tmp_path: Path) -> None:
    from cascade.prompts import build_launch_prompt

    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(
            repo_root=tmp_path / "jungle",
            worktree_root=tmp_path / "jungle-worktrees",
        ),
        commands=CommandsConfig(create_worktree="echo create"),
    )
    agent_state = {"agent": "oc1", "issue": 1, "title": "T", "worktree": "/tmp/wt"}

    prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body="Do it.",
        instruction_files=[],
    )

    assert "Do not inspect or edit unrelated sibling repositories in the workspace." in prompt


# ---------------------------------------------------------------------------
# 8. Legacy config (no workspace_root) still works
# ---------------------------------------------------------------------------


def test_legacy_config_without_workspace_root_still_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
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

    assert project.paths.workspace_root is None
    assert project.paths.repo_root == (tmp_path / "repo").resolve()
    assert resolve_workspace_root(project) is None


def test_legacy_config_validate_paths_without_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
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
    results = validate_project_paths(project)

    # repo_root exists → ok
    repo_result = next(r for r in results if r.key == "repo_root")
    assert repo_result.status == "ok"
    # worktree_root missing → warn (not fail, since workspace_root is None — no escape check)
    wt_result = next(r for r in results if r.key == "worktree_root")
    assert wt_result.status == "warn"
