from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import (
    RepairFinding,
    RepairKind,
    app,
    detect_missing_mandate_metadata,
    mandate_metadata_path,
    format_mandate_start_command,
    repair_missing_workspace_links,
    repair_missing_mandate_metadata,
)


def _write_project_file(
    tmp_path: Path,
    *,
    mandate_start: str | None = None,
    active_branch: str | None = "staging",
) -> Path:
    mandate_block = ""
    if mandate_start is not None:
        mandate_block = f"\n  mandate_start: {mandate_start}"
    branches_block = ""
    if active_branch is not None:
        branches_block = f"\nbranches:\n  active_branch: {active_branch}\n"
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / 'repo'}
  worktree_root: {tmp_path / 'worktrees'}
commands:
  create_worktree: echo create{mandate_block}
  preflight: make mandate-preflight MANDATE_SLUG={{slug}}
{branches_block}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def _setup_agent(
    tmp_path: Path,
    *,
    agent: str = "a1",
    slug: str = "enrich-audit-log-messages",
    title: str = "Enrich Audit Log Messages",
    with_mandates_dir: bool = True,
    with_metadata_file: bool = False,
    mandate_start: str | None = "make mandate-start MANDATE_SLUG={slug} MANDATE_TITLE={title_shell} MANDATE_CANONICAL_MANDATE={canonical_mandate_shell} MANDATE_ACTIVE_BRANCH={active_branch_shell}",
    active_branch: str | None = "staging",
) -> tuple[Path, Path, Path, dict[str, object]]:
    monkey_repo = tmp_path / "repo"
    monkey_repo.mkdir(parents=True, exist_ok=True)
    project_file = _write_project_file(
        tmp_path,
        mandate_start=mandate_start,
        active_branch=active_branch,
    )

    worktree = tmp_path / "worktrees" / f"{agent}-{slug}"
    worktree.mkdir(parents=True, exist_ok=True)
    if with_mandates_dir:
        (worktree / ".github" / "mandates").mkdir(parents=True, exist_ok=True)
    if with_metadata_file:
        (worktree / ".github" / "mandates" / f"{slug}.json").write_text("{}\n", encoding="utf-8")

    run_dir = tmp_path / "state" / "jungle" / "runs" / agent
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mandate.md").write_text("# Mandate\n\nCanonical mandate body", encoding="utf-8")

    state = {
        "project": "jungle",
        "agent": agent,
        "issue": 45,
        "title": title,
        "slug": slug,
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }
    state_path = tmp_path / "state" / "jungle" / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    project_config = cli_module.load_project_config(project_file)
    return project_file, worktree, run_dir, {**state, "project": project_config.name}


def _setup_workspace_link_agent(tmp_path: Path, *, link_template: str, target_template: str) -> tuple[Path, dict[str, object]]:
    project_file = tmp_path / "project_workspace_links.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: "."
  repo_root: "repo"
  worktree_root: "jungle-worktrees"
  secrets_root: "jungle-secrets"
workspace_links:
  - link: "{link_template}"
    target: "{target_template}"
commands:
  create_worktree: echo create
  preflight: make mandate-preflight MANDATE_SLUG={{slug}}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-worktrees").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-secrets" / "instica").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-secrets" / "instica" / ".env.local").write_text("TOKEN=1\n", encoding="utf-8")

    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 45,
        "title": "Workspace Link Test",
        "slug": "workspace-link-test",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(tmp_path / "jungle-worktrees" / "a1-workspace-link-test"),
        "run_dir": str(tmp_path / "state" / "jungle" / "runs" / "a1"),
        "project_file": str(project_file),
    }
    Path(str(state["run_dir"])).mkdir(parents=True, exist_ok=True)
    project_config = cli_module.load_project_config(project_file)
    return project_file, {**state, "project": project_config.name}


def test_detect_missing_mandate_metadata_missing_and_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=False)
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    finding = detect_missing_mandate_metadata(project_config, agent_state)
    assert finding is not None
    assert finding.kind == RepairKind.missing_mandate_metadata
    assert finding.can_repair is True

    (_worktree / ".github" / "mandates" / f"{agent_state['slug']}.json").write_text("{}\n", encoding="utf-8")
    finding_after = detect_missing_mandate_metadata(project_config, agent_state)
    assert finding_after is None


def test_format_mandate_start_command_uses_shell_quoting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, _worktree, run_dir, agent_state = _setup_agent(
        tmp_path,
        title="Enrich Audit Log Messages (Phase 1)",
        mandate_start="make mandate-start MANDATE_SLUG={slug} MANDATE_TITLE={title_shell} MANDATE_CANONICAL_MANDATE={canonical_mandate_shell} MANDATE_ACTIVE_BRANCH={active_branch_shell}",
    )
    project_config = cli_module.load_project_config(project_file)
    cmd = format_mandate_start_command(
        project_config,
        agent=str(agent_state["agent"]),
        slug=str(agent_state["slug"]),
        issue=int(agent_state["issue"]),
        title=str(agent_state["title"]),
        active_branch="staging",
        canonical_mandate=run_dir / "mandate.md",
    )

    assert cmd is not None
    assert "MANDATE_SLUG=enrich-audit-log-messages" in cmd
    assert "MANDATE_TITLE='Enrich Audit Log Messages (Phase 1)'" in cmd
    assert "MANDATE_CANONICAL_MANDATE=" in cmd
    assert "MANDATE_ACTIVE_BRANCH=staging" in cmd


def test_repair_cli_active_branch_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path, active_branch="main")

    recorded_override: dict[str, str | None] = {"value": None}

    def _run_repair(project_config, agent_state, *, kind, dry_run, allow_stash, active_branch_override, file_path=None):
        recorded_override["value"] = active_branch_override
        run_dir = Path(str(agent_state["run_dir"]))
        return cli_module.RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=True,
            dry_run=False,
            message="Repair completed successfully.",
            log_path=run_dir / "repair_missing_mandate_metadata.log",
        )

    monkeypatch.setattr(cli_module, "run_repair", _run_repair)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["repair", "a1", "--project", "jungle", "--active-branch", "staging"],
    )

    assert result.exit_code == 0, result.output
    assert recorded_override["value"] == "staging"


def test_check_and_repair_use_same_detector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=False)

    calls: list[str] = []

    def _detect(project_config, state, *, active_branch_override=None):
        calls.append(str(state.get("agent", "")))
        return RepairFinding(
            kind=RepairKind.missing_mandate_metadata,
            slug=str(state["slug"]),
            title=str(state["title"]),
            worktree=worktree,
            metadata_path=mandate_metadata_path(worktree, str(state["slug"])),
            canonical_mandate_path=run_dir / "mandate.md",
            message="Required mandate metadata is missing.",
            can_repair=True,
            repair_command="make mandate-start MANDATE_SLUG=enrich-audit-log-messages MANDATE_ACTIVE_BRANCH=staging",
        )

    def _run_command(cmd: str, cwd: Path | None = None):
        if "make mandate-start" in cmd:
            (worktree / ".github" / "mandates" / "enrich-audit-log-messages.json").write_text("{}\n", encoding="utf-8")

        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "detect_missing_mandate_metadata", _detect)
    monkeypatch.setattr(cli_module, "run_command", _run_command)
    monkeypatch.setattr(cli_module, "diff", lambda agent, project, save=True: None)

    runner = CliRunner()
    check_result = runner.invoke(app, ["check", "a1", "--project", "jungle"])
    repair_result = runner.invoke(app, ["repair", "a1", "--project", "jungle", "--active-branch", "staging"])

    assert check_result.exit_code != 0
    assert repair_result.exit_code == 0, repair_result.output
    assert len(calls) >= 2


def test_missing_active_branch_on_agent_branch_has_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, active_branch=None)
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    monkeypatch.setattr(
        cli_module,
        "get_current_branch",
        lambda _worktree: "agent/a1/enrich-audit-log-messages",
    )

    finding = detect_missing_mandate_metadata(project_config, agent_state)
    assert finding is not None
    assert finding.can_repair is False
    assert (
        finding.message
        == "Active branch is required for mandate_start. Configure branches.active_branch or pass --active-branch."
    )


def test_existing_metadata_produces_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    def _run_command(cmd: str, cwd: Path | None = None):
        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run_command)

    result = repair_missing_mandate_metadata(project_config, agent_state, dry_run=False, allow_stash=True)

    assert result.success is True
    assert "metadata" in result.message.lower()


def test_repair_recreates_and_stages_metadata_then_check_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.chdir(tmp_path)
    _project_file, worktree, run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=False)

    subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, capture_output=True, check=True)

    mandates_dir = worktree / ".github" / "mandates"
    mandates_dir.mkdir(parents=True, exist_ok=True)
    gitkeep = mandates_dir / ".gitkeep"
    gitkeep.write_text("\n", encoding="utf-8")
    subprocess.run(["git", "add", ".github/mandates/.gitkeep"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init mandates dir"], cwd=worktree, capture_output=True, check=True)

    original_run_command = cli_module.run_command

    def _run_command(cmd: str, cwd: Path | None = None):
        if "make mandate-start" in cmd:
            # Simulate buggy mandate-start behavior: command succeeds but does not create metadata JSON.
            (worktree / ".github" / "mandates" / "audit.log").write_text(
                "audit entry\n",
                encoding="utf-8",
            )

            class _R:
                stdout = ""

            return _R()
        return original_run_command(cmd, cwd=cwd)

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False):
        _ = (verbose, watch)
        metadata_rel = f".github/mandates/{agent_state['slug']}.json"
        audit_rel = ".github/mandates/audit.log"
        metadata_abs = worktree / metadata_rel

        if not metadata_abs.exists():
            gate_result = {
                "timestamp": "2026-04-23T00:00:00Z",
                "command": "make mandate-preflight",
                "exit_code": 2,
                "passed": False,
                "log_path": str(run_dir / "preflight.log"),
                "git_head_sha": "(unknown)",
                "diff_fingerprint": "(unknown)",
                "touched_files": [],
            }
            (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
            (run_dir / "preflight.log").write_text(
                f"Required mandate metadata is missing: {metadata_abs}\n",
                encoding="utf-8",
            )
            raise typer.Exit(1)

        status_output = subprocess.run(
            ["git", "status", "--porcelain", "--", metadata_rel, audit_rel],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        has_untracked = any(line.startswith("??") for line in status_output.splitlines())
        if has_untracked:
            gate_result = {
                "timestamp": "2026-04-23T00:00:00Z",
                "command": "make mandate-preflight",
                "exit_code": 2,
                "passed": False,
                "log_path": str(run_dir / "preflight.log"),
                "git_head_sha": "(unknown)",
                "diff_fingerprint": "(unknown)",
                "touched_files": [],
            }
            (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
            (run_dir / "preflight.log").write_text(
                "error: pathspec '.github/mandates/enrich-audit-log-messages.json' did not match any files\n",
                encoding="utf-8",
            )
            raise typer.Exit(1)

        gate_result = {
            "timestamp": "2026-04-23T00:00:00Z",
            "command": "make mandate-preflight",
            "exit_code": 0,
            "passed": True,
            "log_path": str(run_dir / "preflight.log"),
            "git_head_sha": "(unknown)",
            "diff_fingerprint": "(unknown)",
            "touched_files": [],
        }
        (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
        (run_dir / "preflight.log").write_text("preflight passed\n", encoding="utf-8")

    monkeypatch.setattr(cli_module, "run_command", _run_command)
    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "diff", lambda agent, project, save=True: None)

    runner = CliRunner()
    result = runner.invoke(app, ["check", "a1", "--project", "jungle", "--repair"])

    assert result.exit_code == 0, result.output
    assert "Required mandate metadata is missing" not in result.output

    metadata_path = worktree / ".github" / "mandates" / f"{agent_state['slug']}.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["slug"] == agent_state["slug"]
    assert payload["status"] == "in_progress"
    assert payload["repo"] == "jungle"
    assert payload["agent_branch"] == f"agent/{agent_state['agent']}/{agent_state['slug']}"
    assert payload["active_branch"] == "staging"
    assert payload["worktree_path"] == str(worktree.resolve())
    assert payload["canonical_mandate"] == str((run_dir / "mandate.md").resolve())
    assert isinstance(payload["mandate_id"], str)
    assert payload["mandate_id"].strip()
    assert "github_project_item_id" in payload
    assert isinstance(payload["file_scope"], list)
    assert isinstance(payload["commits"], list)
    assert payload["precommit_failures"] == 0

    cached_names = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f".github/mandates/{agent_state['slug']}.json" in cached_names
    assert ".github/mandates/audit.log" in cached_names

    follow_up = runner.invoke(app, ["check", "a1", "--project", "jungle"])
    assert follow_up.exit_code == 0, follow_up.output


def test_repair_dirty_worktree_stashes_then_restores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path)
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    calls: list[str] = []

    def _run_command(cmd: str, cwd: Path | None = None):
        calls.append(cmd)
        if "git status --porcelain -- .github/mandates/" in cmd:
            class _R:
                stdout = "M  .github/mandates/enrich-audit-log-messages.json\n"

            return _R()
        if "git status --porcelain" in cmd:
            class _R:
                stdout = " M changed.py\n"

            return _R()
        if "git stash list" in cmd:
            class _R:
                stdout = "stash@{0}\n"

            return _R()
        if "make mandate-start" in cmd:
            (worktree / ".github" / "mandates" / f"{agent_state['slug']}.json").write_text("{}\n", encoding="utf-8")
        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run_command)
    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("repair must not call OpenCode")),
    )

    result = repair_missing_mandate_metadata(project_config, agent_state, dry_run=False, allow_stash=True)

    assert result.success is True
    assert any("git stash push -u" in cmd for cmd in calls)
    assert any("git stash pop" in cmd for cmd in calls)
    assert not any("git stash drop" in cmd for cmd in calls)
    assert not any("git push" in cmd for cmd in calls)


def test_repair_clean_worktree_runs_without_stash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path)
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    calls: list[str] = []

    def _run_command(cmd: str, cwd: Path | None = None):
        calls.append(cmd)
        if "git status --porcelain" in cmd:
            class _R:
                stdout = ""

            return _R()
        if "make mandate-start" in cmd:
            (worktree / ".github" / "mandates" / f"{agent_state['slug']}.json").write_text("{}\n", encoding="utf-8")
        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run_command)
    result = repair_missing_mandate_metadata(project_config, agent_state, dry_run=False, allow_stash=True)

    assert result.success is True
    assert any("make mandate-start" in cmd for cmd in calls)
    assert not any("git stash push -u" in cmd for cmd in calls)


def test_check_with_repair_repairs_then_runs_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, run_dir, agent_state = _setup_agent(tmp_path)

    seen_active_branch: dict[str, str | None] = {"value": None}

    def _repair(project_config, state, *, dry_run=False, allow_stash=True, active_branch_override=None):
        seen_active_branch["value"] = active_branch_override
        (worktree / ".github" / "mandates" / f"{state['slug']}.json").write_text("{}\n", encoding="utf-8")
        return cli_module.RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=True,
            dry_run=False,
            message="Repair completed successfully.",
            log_path=run_dir / "repair_missing_mandate_metadata.log",
        )

    preflight_called = {"called": False}

    def _preflight(agent: str, project: str):
        preflight_called["called"] = True
        gate_result = {
            "timestamp": "2026-04-22T12:00:00Z",
            "command": "make mandate-preflight MANDATE_SLUG=enrich-audit-log-messages",
            "exit_code": 0,
            "passed": True,
            "log_path": str(run_dir / "preflight.log"),
            "git_head_sha": "(unknown)",
            "diff_fingerprint": "(unknown)",
            "touched_files": [],
        }
        (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")

    monkeypatch.setattr(cli_module, "repair_missing_mandate_metadata", _repair)
    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "diff", lambda agent, project, save=True: None)
    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("check --repair must not call OpenCode")),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["check", "a1", "--project", "jungle", "--repair", "--active-branch", "staging"],
    )

    assert result.exit_code == 0, result.output
    assert preflight_called["called"] is True
    assert seen_active_branch["value"] == "staging"


def test_repair_kind_forces_missing_metadata_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    seen_kind: dict[str, RepairKind | None] = {"value": None}

    def _run_repair(project_config, agent_state, *, kind, dry_run, allow_stash, active_branch_override, file_path=None):
        seen_kind["value"] = kind
        run_dir = Path(str(agent_state["run_dir"]))
        return cli_module.RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=True,
            dry_run=False,
            message="Repair completed successfully.",
            log_path=run_dir / "repair_missing_mandate_metadata.log",
        )

    monkeypatch.setattr(cli_module, "run_repair", _run_repair)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "repair",
            "a1",
            "--project",
            "jungle",
            "--kind",
            "missing-mandate-metadata",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen_kind["value"] == RepairKind.missing_mandate_metadata


def test_repair_kind_mandate_metadata_updates_stale_status_and_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, _run_dir, _agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    metadata_path = worktree / ".github" / "mandates" / "enrich-audit-log-messages.json"
    metadata_path.write_text(
        json.dumps(
            {
                "slug": "enrich-audit-log-messages",
                "status": "done",
                "repo": "wrong-repo-name",
                "agent_branch": "agent/a1/enrich-audit-log-messages",
                "active_branch": "main",
                "worktree_path": "/tmp/wrong",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def _run_command(cmd: str, cwd: Path | None = None):
        if "git status --porcelain -- .github/mandates/" in cmd:
            class _R:
                stdout = "M  .github/mandates/enrich-audit-log-messages.json\n"

            return _R()

        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run_command)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "repair",
            "a1",
            "--project",
            "jungle",
            "--kind",
            "mandate-metadata",
            "--active-branch",
            "staging",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["status"] == "in_progress"
    assert payload["repo"] == "jungle"
    assert payload["active_branch"] == "staging"
    assert payload["worktree_path"] == str(worktree.resolve())


def test_repair_passes_active_branch_into_mandate_start_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, active_branch="main")
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    commands: list[str] = []

    def _run_command(cmd: str, cwd: Path | None = None):
        commands.append(cmd)
        if "git status --porcelain" in cmd:
            class _R:
                stdout = ""

            return _R()
        if "make mandate-start" in cmd:
            (worktree / ".github" / "mandates" / f"{agent_state['slug']}.json").write_text("{}\n", encoding="utf-8")
        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run_command)

    result = repair_missing_mandate_metadata(
        project_config,
        agent_state,
        dry_run=False,
        allow_stash=True,
        active_branch_override="staging",
    )

    assert result.success is True
    mandate_cmds = [cmd for cmd in commands if "make mandate-start" in cmd]
    assert mandate_cmds
    assert "MANDATE_ACTIVE_BRANCH=staging" in mandate_cmds[0]


def test_check_without_repair_prints_repair_suggestion_no_make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "diff", lambda agent, project, save=True: None)
    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("check must not call OpenCode")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["check", "a1", "--project", "jungle"])
    normalized = " ".join(result.output.split())

    assert result.exit_code != 0
    assert "Repair available: cascade repair a1 --project jungle" in normalized
    assert "make mandate-start" not in normalized


def test_claim_fails_clearly_when_mandate_start_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = _write_project_file(
        tmp_path,
        mandate_start="make mandate-start MANDATE_SLUG={slug} MANDATE_TITLE={title_shell} MANDATE_CANONICAL_MANDATE={canonical_mandate_shell}",
    )
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)

    def _fetch_issue(owner: str, repo: str, issue: int):
        return {"title": "Enrich Audit Log Messages", "body": "Mandate body", "number": issue}

    def _run_command(cmd: str, cwd: Path | None = None):
        if "agent-worktree-create" in cmd or "echo create" in cmd:
            worktree = tmp_path / "worktrees" / "a1-enrich-audit-log-messages"
            (worktree / ".github" / "mandates").mkdir(parents=True, exist_ok=True)
            class _R:
                stdout = ""

            return _R()
        if "make mandate-start" in cmd:
            raise cli_module.CommandError(cmd=cmd, cwd=cwd, exit_code=2, output="failed")
        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "fetch_issue", _fetch_issue)
    monkeypatch.setattr(cli_module, "run_command", _run_command)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "claim",
            "--project-file",
            str(project_file),
            "--issue",
            "45",
            "--agent",
            "a1",
            "--model",
            "openrouter/z-ai/glm-4.7-flash",
        ],
    )

    assert result.exit_code != 0
    assert "Failed to initialize mandate metadata" in result.output or "Repair command failed" in result.output


def test_missing_workspace_link_repair_creates_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, agent_state = _setup_workspace_link_agent(
        tmp_path,
        link_template="{worktree_root}/jungle-secrets",
        target_template="{secrets_root}",
    )
    project_config = cli_module.load_project_config(project_file)

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("workspace link repair must not call OpenCode")),
    )

    result = repair_missing_workspace_links(project_config, agent_state, dry_run=False)
    link = tmp_path / "jungle-worktrees" / "jungle-secrets"

    assert result.success is True
    assert link.is_symlink()
    assert link.resolve() == (tmp_path / "jungle-secrets").resolve()


def test_existing_correct_workspace_symlink_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, agent_state = _setup_workspace_link_agent(
        tmp_path,
        link_template="{worktree_root}/jungle-secrets",
        target_template="{secrets_root}",
    )
    project_config = cli_module.load_project_config(project_file)

    link = tmp_path / "jungle-worktrees" / "jungle-secrets"
    link.symlink_to((tmp_path / "jungle-secrets").resolve())

    result = repair_missing_workspace_links(project_config, agent_state, dry_run=False)

    assert result.success is True
    assert "already correct" in result.message.lower()


def test_existing_wrong_workspace_symlink_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, agent_state = _setup_workspace_link_agent(
        tmp_path,
        link_template="{worktree_root}/jungle-secrets",
        target_template="{secrets_root}",
    )
    project_config = cli_module.load_project_config(project_file)

    wrong_target = tmp_path / "wrong-secrets"
    wrong_target.mkdir(parents=True, exist_ok=True)
    link = tmp_path / "jungle-worktrees" / "jungle-secrets"
    link.symlink_to(wrong_target.resolve())

    result = repair_missing_workspace_links(project_config, agent_state, dry_run=False)

    assert result.success is False
    assert "points elsewhere" in result.message.lower()


def test_missing_workspace_link_target_stops_with_clear_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, agent_state = _setup_workspace_link_agent(
        tmp_path,
        link_template="{worktree_root}/jungle-secrets",
        target_template="{secrets_root}",
    )
    project_config = cli_module.load_project_config(project_file)

    target = tmp_path / "jungle-secrets"
    for child in sorted(target.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
    for child in sorted(target.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    target.rmdir()

    result = repair_missing_workspace_links(project_config, agent_state, dry_run=False)

    assert result.success is False
    assert "target does not exist" in result.message.lower()


def test_workspace_link_paths_escaping_workspace_root_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, agent_state = _setup_workspace_link_agent(
        tmp_path,
        link_template="{workspace_root}/../escape-link",
        target_template="{secrets_root}",
    )
    project_config = cli_module.load_project_config(project_file)

    result = repair_missing_workspace_links(project_config, agent_state, dry_run=False)

    assert result.success is False
    assert "escapes workspace_root" in result.message.lower()


def test_gate_summary_shows_workflow_repair_for_missing_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["gate-summary", "a1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.lower().split())
    assert "mandate-metadata" in output
    assert "workflow" in output
    assert "model recommended" in output
    assert " no " in f" {output} "
    assert "cascade repair a1 --project jungle" in output


# ---------------------------------------------------------------------------
# Dirty file repair tests
# ---------------------------------------------------------------------------


def _write_project_file_with_dirty_file_repairs(
    tmp_path: Path,
    *,
    auto_revert_tracked: list[str] | None = None,
    never_revert: list[str] | None = None,
) -> Path:
    auto_revert_block = ""
    never_revert_block = ""
    
    if auto_revert_tracked:
        patterns = "\n    ".join(f"- {p}" for p in auto_revert_tracked)
        auto_revert_block = f"\n  auto_revert_tracked:\n    {patterns}"
    else:
        auto_revert_block = "\n  auto_revert_tracked: []"
    
    if never_revert:
        patterns = "\n    ".join(f"- {p}" for p in never_revert)
        never_revert_block = f"\n  never_revert:\n    {patterns}"
    else:
        never_revert_block = "\n  never_revert: []"
    
    dirty_file_repairs_block = f"\ndirty_file_repairs:{auto_revert_block}{never_revert_block}\n"
    
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / 'repo'}
  worktree_root: {tmp_path / 'worktrees'}
commands:
  create_worktree: echo create
  preflight: make mandate-preflight MANDATE_SLUG={{slug}}
branches:
  active_branch: staging
{dirty_file_repairs_block}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def test_dirty_file_repair_safe_tracked_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a tracked file matching auto_revert_tracked is successfully reverted."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    
    # Setup worktree with git repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, check=True)
    
    worktree = tmp_path / "worktrees" / "a1-test"
    worktree.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, capture_output=True, check=True)
    
    # Create and commit a tracked file
    test_file = worktree / "scripts" / "ensure_docker_desktop.sh"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("#!/bin/bash\n# initial content\n")
    subprocess.run(["git", "add", str(test_file)], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, capture_output=True, check=True)
    
    # Modify the file (make it dirty)
    test_file.write_text("#!/bin/bash\n# modified content\n")
    
    # Setup project config and agent state
    project_file = _write_project_file_with_dirty_file_repairs(
        tmp_path,
        auto_revert_tracked=["scripts/ensure_docker_desktop.sh"],
        never_revert=[".github/mandates/**"],
    )
    
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    
    agent_state = {
        "project": "jungle",
        "agent": "a1",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
    }
    
    project_config = cli_module.load_project_config(project_file)
    result = cli_module.repair_dirty_tracked_file(
        project_config,
        agent_state,
        file_path="scripts/ensure_docker_desktop.sh",
        dry_run=False,
    )
    
    assert result.success is True
    assert "reverted" in result.message.lower() or "checkout" in result.message.lower()
    
    # Verify file was actually reverted
    assert test_file.read_text() == "#!/bin/bash\n# initial content\n"


def test_dirty_file_repair_never_revert_pattern(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that files matching never_revert are not reverted."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    
    # Setup worktree with git repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, check=True)
    
    worktree = tmp_path / "worktrees" / "a1-test"
    worktree.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, capture_output=True, check=True)
    
    # Create and commit a file
    test_file = worktree / "pyproject.toml"
    test_file.write_text("[tool.pytest]\n# initial\n")
    subprocess.run(["git", "add", str(test_file)], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, capture_output=True, check=True)
    
    # Modify the file
    test_file.write_text("[tool.pytest]\n# modified\n")
    
    # Setup project config with pyproject.toml in never_revert
    project_file = _write_project_file_with_dirty_file_repairs(
        tmp_path,
        auto_revert_tracked=["pyproject.toml"],
        never_revert=["pyproject.toml", ".github/mandates/**"],
    )
    
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    
    agent_state = {
        "project": "jungle",
        "agent": "a1",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
    }
    
    project_config = cli_module.load_project_config(project_file)
    result = cli_module.repair_dirty_tracked_file(
        project_config,
        agent_state,
        file_path="pyproject.toml",
        dry_run=False,
    )
    
    assert result.success is False
    assert "never_revert" in result.message.lower() or "will not revert" in result.message.lower()
    
    # Verify file was NOT reverted
    assert "[tool.pytest]\n# modified\n" in test_file.read_text()


def test_dirty_file_repair_untracked_file_not_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that untracked files are not deleted by default."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, check=True)
    
    worktree = tmp_path / "worktrees" / "a1-test"
    worktree.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, capture_output=True, check=True)
    
    # Create an untracked file
    test_file = worktree / "scripts" / "untracked.sh"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("#!/bin/bash\n# untracked\n")
    
    # Setup project config
    project_file = _write_project_file_with_dirty_file_repairs(
        tmp_path,
        auto_revert_tracked=["scripts/untracked.sh"],
        never_revert=[],
    )
    
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    
    agent_state = {
        "project": "jungle",
        "agent": "a1",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
    }
    
    project_config = cli_module.load_project_config(project_file)
    result = cli_module.repair_dirty_tracked_file(
        project_config,
        agent_state,
        file_path="scripts/untracked.sh",
        dry_run=False,
    )
    
    assert result.success is False
    assert "not tracked" in result.message.lower()
    
    # Verify file still exists (wasn't deleted)
    assert test_file.exists()


def test_repair_cli_dirty_file_kind_requires_file_param(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that --kind dirty-file requires --file parameter."""
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, _run_dir, _agent_state = _setup_agent(tmp_path)
    
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["repair", "a1", "--project", "jungle", "--kind", "dirty-file"],
    )
    
    assert result.exit_code != 0
    assert "--file is required" in result.output or "required" in result.output.lower()


def test_repair_docker_runtime_network_runs_scoped_compose_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(_project_file)

    commands: list[str] = []

    def _run_command(command: str, cwd: Path | None = None):
        commands.append(command)

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr(cli_module, "run_command", _run_command)

    log_text = (
        "Error response from daemon: error while removing network: network "
        "jungle-sample_default has active endpoints\n"
    )
    result = cli_module.run_repair(
        project_config,
        agent_state,
        kind=cli_module.RepairKind.docker_runtime_network,
        dry_run=False,
        allow_stash=True,
        active_branch_override=None,
        runtime_log_text=log_text,
    )

    assert result.success is True
    assert result.kind == cli_module.RepairKind.docker_runtime_network
    assert any("docker compose -p jungle-sample down --remove-orphans" in command for command in commands)
    assert any("label=com.docker.compose.project=jungle-sample" in command for command in commands)
    assert not any("docker system prune" in command for command in commands)
    assert not any("docker network prune" in command for command in commands)


def test_repair_docker_runtime_network_fails_without_compose_project_in_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(_project_file)

    result = cli_module.run_repair(
        project_config,
        agent_state,
        kind=cli_module.RepairKind.docker_runtime_network,
        dry_run=False,
        allow_stash=True,
        active_branch_override=None,
        runtime_log_text="docker failed without project name",
    )

    assert result.success is False
    assert "Unable to identify compose project name" in result.message


def _init_git_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, capture_output=True, check=True)


def _write_mandate_payload(
    worktree: Path,
    slug: str,
    *,
    file_scope: list[str],
    active_branch: str = "staging",
) -> None:
    payload = {
        "slug": slug,
        "mandate_id": "JNG-04232026-001",
        "agent_branch": f"agent/a1/{slug}",
        "active_branch": active_branch,
        "repo": "jungle",
        "worktree_path": str(worktree.resolve()),
        "canonical_mandate": str((worktree / "mandate.md").resolve()),
        "file_scope": file_scope,
        "commits": [],
        "precommit_failures": 0,
        "created_at": "2026-04-23T00:00:00Z",
        "updated_at": "2026-04-23T00:00:00Z",
    }
    metadata_path = worktree / ".github" / "mandates" / f"{slug}.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_closeout_prep_stages_expected_mandate_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    (worktree / "api").mkdir(parents=True, exist_ok=True)
    (worktree / "api" / "service.py").write_text("value = 1\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    (worktree / "mandate.md").write_text("# Mandate\n", encoding="utf-8")
    _write_mandate_payload(worktree, str(agent_state["slug"]), file_scope=["api/**"]) 

    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)

    (worktree / "api" / "service.py").write_text("value = 2\n", encoding="utf-8")

    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    assert result.success is True
    assert "staged" in result.message.lower()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True, check=True).stdout
    assert "M  api/service.py" in status
    assert "test_debug.py" not in status
    assert run_dir.joinpath("closeout_prep.log").exists()


def test_closeout_prep_flags_suspicious_extras_and_leaves_them_unstaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    (worktree / "api").mkdir(parents=True, exist_ok=True)
    (worktree / "api" / "service.py").write_text("value = 1\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    (worktree / "mandate.md").write_text("# Mandate\n", encoding="utf-8")
    _write_mandate_payload(worktree, str(agent_state["slug"]), file_scope=["api/**"]) 

    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)

    (worktree / "api" / "service.py").write_text("value = 3\n", encoding="utf-8")
    (worktree / "test_debug.py").write_text("print('debug')\n", encoding="utf-8")

    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    assert result.success is False
    assert "suspicious" in result.message.lower()
    assert "test_debug.py" in result.message

    status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True, check=True).stdout
    assert "M  api/service.py" in status
    assert "?? test_debug.py" in status


def test_closeout_prep_commit_requires_yes_and_enforces_mandate_id_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    (worktree / "api").mkdir(parents=True, exist_ok=True)
    (worktree / "api" / "service.py").write_text("value = 1\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    (worktree / "mandate.md").write_text("# Mandate\n", encoding="utf-8")
    _write_mandate_payload(worktree, str(agent_state["slug"]), file_scope=["api/**"]) 

    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)

    (worktree / "api" / "service.py").write_text("value = 4\n", encoding="utf-8")

    blocked = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=True,
        yes=False,
        dry_run=False,
        commit_message="implementation follow-up",
    )
    assert blocked.success is False
    assert "--yes" in blocked.message

    committed = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=True,
        yes=True,
        dry_run=False,
        commit_message="implementation follow-up",
    )
    assert committed.success is True
    assert "committed" in committed.message.lower()

    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject.startswith("JNG-04232026-001")
    assert "implementation follow-up" in subject


def test_closeout_prep_cli_shows_suggested_command_for_suspicious_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path, with_metadata_file=True)

    def _prep(*args: object, **kwargs: object) -> cli_module.RepairResult:
        return cli_module.RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=False,
            dry_run=False,
            message="Suspicious files: test_debug.py",
            log_path=tmp_path / "state" / "jungle" / "runs" / "a1" / "closeout_prep.log",
        )

    monkeypatch.setattr(cli_module, "prepare_mandate_closeout_dirty_files", _prep)

    runner = CliRunner()
    result = runner.invoke(app, ["closeout-prep", "a1", "--project", "jungle", "--stage"])

    assert result.exit_code != 0
    assert "Suggested next step" in result.output
    assert "cascade closeout-prep a1 --project jungle --stage" in result.output


def test_closeout_prep_auto_fix_gates_runs_gate_fix_and_retries_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path, with_metadata_file=True)

    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    calls = {"prep": 0, "gate_fix": 0}

    def _prep(*args: object, **kwargs: object) -> cli_module.RepairResult:
        calls["prep"] += 1
        if calls["prep"] == 1:
            return cli_module.RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=False,
                dry_run=False,
                message="Commit failed during closeout-prep.",
                log_path=run_dir / "closeout_prep.log",
            )
        return cli_module.RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=True,
            dry_run=False,
            message="Mandate-owned files staged and committed.",
            log_path=run_dir / "closeout_prep.log",
        )

    def _resolve_source(*, run_dir: Path, explicit_context_file: Path | None):
        return {
            "source": "closeout-prep-commit",
            "command": "git commit -m 'JNG-04232026-001 checkpoint'",
            "hook": "backend-docstring",
            "log": "D103 Missing docstring",
        }

    def _gate_fix(*args: object, **kwargs: object) -> None:
        calls["gate_fix"] += 1
        raise typer.Exit(0)

    monkeypatch.setattr(cli_module, "prepare_mandate_closeout_dirty_files", _prep)
    monkeypatch.setattr(cli_module, "_resolve_gate_fix_failure_source", _resolve_source)
    monkeypatch.setattr(cli_module, "gate_fix", _gate_fix)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "closeout-prep",
            "a1",
            "--project",
            "jungle",
            "--stage",
            "--commit",
            "--yes",
            "--auto-fix-gates",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["gate_fix"] == 1
    assert calls["prep"] == 2
    assert "Closeout prep retry log" in result.output


# Regression tests for multi-signal file classification


def test_closeout_prep_multi_signal_recognizes_branch_changed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove that files outside scope are treated as suspicious by conservative classification (not fresh mandate)."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    
    # Create base implementation structure
    (worktree / "jungle").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "clients").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "clients" / "ebay.py").write_text("# ebay client\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    
    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, capture_output=True, check=True)
    
    # Create a staging branch (will be active_branch)
    subprocess.run(["git", "checkout", "-b", "staging"], cwd=worktree, capture_output=True, check=True)
    
    # Create agent branch from staging and advance it (make it NOT a fresh mandate)
    subprocess.run(["git", "checkout", "-b", "agent/a3/test"], cwd=worktree, capture_output=True, check=True)
    (worktree / "some_other_file.txt").write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "add", "some_other_file.txt"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "agent work"], cwd=worktree, capture_output=True, check=True)
    
    # Mandate scope is deliberately incomplete - doesn't include jungle/clients/
    _write_mandate_payload(
        worktree,
        str(agent_state["slug"]),
        file_scope=["docs/**"],  # Deliberately missing jungle/clients/**
        active_branch="staging",
    )
    
    # Modify files outside scope
    (worktree / "jungle" / "clients" / "ebay.py").write_text("# ebay client v2\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\nmodified\n", encoding="utf-8")
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Should fail: ebay.py doesn't match file_scope and doesn't match heuristics,
    # so it's treated as suspicious (conservative approach)
    assert result.success is False
    assert "suspicious" in result.message.lower()
    assert "ebay.py" in result.message


def test_closeout_prep_multi_signal_only_flags_scratch_files_as_suspicious(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove that only obvious scratch files are flagged suspicious, not real implementation files."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    
    # Create implementation structure
    (worktree / "jungle").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "operations.py").write_text("# operations\n", encoding="utf-8")
    (worktree / "jungle" / "test_operations.py").write_text("# test ops\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    
    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)
    
    # Mandate scope covers jungle/ so files should be recognized
    _write_mandate_payload(
        worktree,
        str(agent_state["slug"]),
        file_scope=["jungle/**"],
        active_branch="master",
    )
    
    # Modify mandate files + add obvious scratch files
    (worktree / "jungle" / "operations.py").write_text("# ops v2\n", encoding="utf-8")
    (worktree / "jungle" / "test_operations.py").write_text("# test v2\n", encoding="utf-8")
    (worktree / "test_debug.py").write_text("# debug code\n", encoding="utf-8")
    (worktree / "debug_temp.py").write_text("# temp\n", encoding="utf-8")
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Should fail because suspicious extras exist, but mandate files should be staged
    assert result.success is False
    assert "test_debug.py" in result.message
    assert "debug_temp.py" in result.message
    
    # Verify mandate files were staged despite suspicious files present
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ("M  jungle/operations.py" in status or "A  jungle/operations.py" in status)
    assert ("M  jungle/test_operations.py" in status or "A  jungle/test_operations.py" in status)
    # Debug files should remain untracked (not staged)
    assert "?? test_debug.py" in status or "?? debug_temp.py" in status


def test_closeout_prep_multi_signal_heuristic_same_dir_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove that files in same dir as mandate files are recognized as related."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    
    # Create module with multiple related files
    (worktree / "jungle" / "clients").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "clients" / "ebay_client.py").write_text("# client\n", encoding="utf-8")
    (worktree / "jungle" / "clients" / "ebay_models.py").write_text("# models\n", encoding="utf-8")
    (worktree / "jungle" / "clients" / "test_ebay.py").write_text("# tests\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    
    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)
    
    # Narrow file_scope to only explicitly mention one file
    _write_mandate_payload(
        worktree,
        str(agent_state["slug"]),
        file_scope=["jungle/clients/ebay_client.py"],  # Only this one
        active_branch="master",
    )
    
    # Modify all related files
    (worktree / "jungle" / "clients" / "ebay_client.py").write_text("# client v2\n", encoding="utf-8")
    (worktree / "jungle" / "clients" / "ebay_models.py").write_text("# models v2\n", encoding="utf-8")
    (worktree / "jungle" / "clients" / "test_ebay.py").write_text("# tests v2\n", encoding="utf-8")
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Should succeed because all files in same dir are recognized as related
    assert result.success is True
    assert "staged" in result.message.lower()
    
    # Verify all files in the directory were staged
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ("M  jungle/clients/ebay_client.py" in status or "A  jungle/clients/ebay_client.py" in status)
    assert ("M  jungle/clients/ebay_models.py" in status or "A  jungle/clients/ebay_models.py" in status)
    assert ("M  jungle/clients/test_ebay.py" in status or "A  jungle/clients/test_ebay.py" in status)


def test_closeout_prep_multi_signal_mixed_mandate_and_scratch_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove closeout-prep becomes usable on freshly implemented mandate with mixed files."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    
    # Create initial state
    (worktree / "jungle" / "services").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "services" / "payment.py").write_text("# service\n", encoding="utf-8")
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    
    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, capture_output=True, check=True)
    
    # Mandate scope is broad enough for real files
    _write_mandate_payload(
        worktree,
        str(agent_state["slug"]),
        file_scope=["jungle/**"],
        active_branch="master",
    )
    
    # Realistic scenario: mandate files + test scaffolding + debug artifacts from development
    (worktree / "jungle" / "services" / "payment.py").write_text("# service v2\n", encoding="utf-8")
    (worktree / "jungle" / "services" / "payment_utils.py").write_text("# utils\n", encoding="utf-8")
    (worktree / "jungle" / "services" / "test_payment.py").write_text("# tests\n", encoding="utf-8")
    (worktree / "test_debug.py").write_text("# dev debug\n", encoding="utf-8")
    (worktree / "debug_notes.txt").write_text("notes\n", encoding="utf-8")
    (worktree / ".coverage_temp").write_text("coverage\n", encoding="utf-8")
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Should fail due to suspicious files, but mandate files should be staged
    assert result.success is False
    
    # Verify mandate files are staged
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # Mandate files should be staged (M not ??)
    assert ("M  jungle/services/payment.py" in status or "A  jungle/services/payment.py" in status)
    assert ("M  jungle/services/payment_utils.py" in status or "A  jungle/services/payment_utils.py" in status)
    assert ("M  jungle/services/test_payment.py" in status or "A  jungle/services/test_payment.py" in status)
    
    # Scratch files should remain unstaged
    assert "?? test_debug.py" in status or "?? debug_notes.txt" in status or "?? .coverage_temp" in status
    
    # Now user removes scratch files and tries again
    (worktree / "test_debug.py").unlink()
    (worktree / "debug_notes.txt").unlink()
    (worktree / ".coverage_temp").unlink()
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Now should succeed
    assert result.success is True
    assert "staged" in result.message.lower()


def test_closeout_prep_freshly_implemented_mandate_with_real_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration test: Freshly implemented mandate (HEAD == active_branch) with real git branches.
    
    This reproduces the a3 scenario where:
    - Mandate branch is created from staging but hasn't advanced beyond it
    - Implementation files are dirty (modified/added) but not committed
    - Closeout-prep should detect these as mandate-owned even without committed diff
    """
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    
    # Create base files on staging branch
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)
    
    # Create and switch to staging branch explicitly
    subprocess.run(["git", "checkout", "-b", "staging"], cwd=worktree, capture_output=True, check=True)
    
    # Create and switch to agent branch from staging (same commit as staging)
    subprocess.run(
        ["git", "checkout", "-b", "agent/a3/enrich-audit"],
        cwd=worktree,
        capture_output=True,
        check=True,
    )
    
    # Mandate scope only includes metadata (incomplete scope like real a3)
    _write_mandate_payload(
        worktree,
        str(agent_state["slug"]),
        file_scope=[".github/mandates/audit.log", ".github/mandates/enrich-audit.json"],
        active_branch="staging",
    )
    
    # Simulate freshly implemented mandate: add implementation files (dirty, not committed)
    (worktree / "jungle" / "audit").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "audit" / "__init__.py").write_text("# audit init\n", encoding="utf-8")
    (worktree / "jungle" / "audit" / "messages.py").write_text("# audit messages\n", encoding="utf-8")
    (worktree / "jungle" / "tests").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "tests" / "test_audit.py").write_text("# tests\n", encoding="utf-8")
    (worktree / "api" / "serializers").mkdir(parents=True, exist_ok=True)
    (worktree / "api" / "serializers" / "audit.py").write_text("# serializers\n", encoding="utf-8")
    
    # Also add metadata file change
    (worktree / ".github" / "mandates" / "audit.log").write_text("updated\n", encoding="utf-8")
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Should succeed: freshly-implemented mandate detection should recognize impl files
    assert result.success is True, f"Expected success but got: {result.message}"
    assert "staged" in result.message.lower()
    
    # Verify impl files were staged
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # Implementation files should be staged
    assert ("A  jungle/audit/__init__.py" in status or "M  jungle/audit/__init__.py" in status)
    assert ("A  jungle/audit/messages.py" in status or "M  jungle/audit/messages.py" in status)
    assert ("A  jungle/tests/test_audit.py" in status or "M  jungle/tests/test_audit.py" in status)
    assert ("A  api/serializers/audit.py" in status or "M  api/serializers/audit.py" in status)
    # Metadata should be staged
    assert ("M  .github/mandates/audit.log" in status)


def test_closeout_prep_freshly_implemented_mandate_with_scratch_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freshly implemented mandate with mixed real and scratch files.
    
    Should stage real impl files and block scratch files.
    """
    import subprocess

    monkeypatch.chdir(tmp_path)
    project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path, with_metadata_file=True)
    project_config = cli_module.load_project_config(project_file)

    _init_git_repo(worktree)
    
    # Create base
    (worktree / ".github" / "mandates" / "audit.log").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, capture_output=True, check=True)
    
    # Create branches
    subprocess.run(["git", "checkout", "-b", "staging"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(
        ["git", "checkout", "-b", "agent/a3/enrich-audit"],
        cwd=worktree,
        capture_output=True,
        check=True,
    )
    
    _write_mandate_payload(
        worktree,
        str(agent_state["slug"]),
        file_scope=[".github/mandates/audit.log"],
        active_branch="staging",
    )
    
    # Real implementation files
    (worktree / "jungle" / "audit").mkdir(parents=True, exist_ok=True)
    (worktree / "jungle" / "audit" / "messages.py").write_text("# impl\n", encoding="utf-8")
    (worktree / "api" / "handlers").mkdir(parents=True, exist_ok=True)
    (worktree / "api" / "handlers" / "audit_handler.py").write_text("# handler\n", encoding="utf-8")
    
    # Scratch files (should be blocked)
    (worktree / "test_debug.py").write_text("debug\n", encoding="utf-8")
    (worktree / "debug_temp_notes.txt").write_text("notes\n", encoding="utf-8")
    
    # Metadata change
    (worktree / ".github" / "mandates" / "audit.log").write_text("updated\n", encoding="utf-8")
    
    result = cli_module.prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=True,
        commit=False,
        yes=False,
        dry_run=False,
        commit_message=None,
    )

    # Should fail due to suspicious files, but impl files should be staged
    assert result.success is False
    assert "test_debug.py" in result.message or "debug_temp_notes.txt" in result.message
    
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    
    # Impl files staged
    assert ("A  jungle/audit/messages.py" in status or "M  jungle/audit/messages.py" in status)
    assert ("A  api/handlers/audit_handler.py" in status or "M  api/handlers/audit_handler.py" in status)
    # Scratch files NOT staged
    assert "?? test_debug.py" in status
    assert "?? debug_temp_notes.txt" in status
