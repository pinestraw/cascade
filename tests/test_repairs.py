from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import (
    RepairFinding,
    RepairKind,
    app,
    detect_missing_mandate_metadata,
    mandate_metadata_path,
    format_mandate_start_command,
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

    def _run_repair(project_config, agent_state, *, kind, dry_run, allow_stash, active_branch_override):
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

    result = repair_missing_mandate_metadata(project_config, agent_state, dry_run=False, allow_stash=True)

    assert result.success is True
    assert "already exists" in result.message.lower()
    assert "no repair is needed" in result.message.lower()


def test_repair_dirty_worktree_stashes_then_restores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, _run_dir, agent_state = _setup_agent(tmp_path)
    project_config = cli_module.load_project_config(Path(str(agent_state["project_file"])))

    calls: list[str] = []

    def _run_command(cmd: str, cwd: Path | None = None):
        calls.append(cmd)
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

    def _run_repair(project_config, agent_state, *, kind, dry_run, allow_stash, active_branch_override):
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
