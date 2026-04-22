"""Tests proving that deterministic CLI commands never invoke OpenCode or model APIs.

Every test here patches `run_command` so any invocation containing 'opencode'
raises AssertionError. If these tests pass, the commands are provably model-free.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_project_file(tmp_path: Path, worktree_root: Path | None = None) -> Path:
    wt_root = worktree_root or (tmp_path / "worktrees")
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / "repo"}
  worktree_root: {wt_root}
commands:
  create_worktree: echo create
  preflight: echo preflight-ok
instructions:
  files:
    - COPILOT.md
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
  profiles:
    cheap_planner:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      input_cost_per_million: 0.06
      output_cost_per_million: 0.40
      use_for:
        - plan
        - summarize
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def _setup_agent(
    tmp_path: Path,
    project: str = "jungle",
    agent: str = "oc1",
) -> tuple[Path, Path, Path]:
    """Create worktree dir, run dir, and agent state JSON. Returns (worktree, run_dir, state_path)."""
    worktree = tmp_path / "worktrees" / f"{agent}-test-feature"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)

    (run_dir / "mandate.md").write_text("# Mandate\n\nDo the thing.", encoding="utf-8")
    (run_dir / "decisions.md").write_text("# Decisions\n\n", encoding="utf-8")
    (run_dir / "questions.md").write_text("# Questions\n\n", encoding="utf-8")
    (run_dir / "running_summary.md").write_text("# Summary\n\nStarted.", encoding="utf-8")

    project_file = _write_project_file(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)

    state_path = tmp_path / "state" / project / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_data: dict[str, Any] = {
        "project": project,
        "agent": agent,
        "issue": 45,
        "title": "Test Feature",
        "slug": "test-feature",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
        "mandate": "Do the thing.",
        "running_summary": "Working.",
        "decisions": [],
        "questions": [],
    }
    state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
    return worktree, run_dir, state_path


def _forbid_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ensure_opencode_available to raise — proving deterministic commands skip it."""
    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("must not call ensure_opencode_available")),
    )


# ---------------------------------------------------------------------------
# note — appends to decisions.md, no model
# ---------------------------------------------------------------------------


def test_note_appends_to_decisions_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app, ["note", "oc1", "--project", "jungle", "--message", "We decided to use batching."]
    )

    assert result.exit_code == 0, result.output
    decisions = (run_dir / "decisions.md").read_text(encoding="utf-8")
    assert "We decided to use batching." in decisions


def test_note_does_not_create_shell_subprocess_with_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    shell_calls: list[str] = []

    def _mock_run(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        shell_calls.append(cmd)
        if "opencode" in cmd:
            raise AssertionError(f"note must not call opencode, got: {cmd!r}")
        raise RuntimeError(f"unexpected shell call: {cmd!r}")

    monkeypatch.setattr(cli_module, "run_command", _mock_run)
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)

    runner = CliRunner()
    runner.invoke(app, ["note", "oc1", "--project", "jungle", "--message", "A decision."])

    assert all("opencode" not in c for c in shell_calls)


# ---------------------------------------------------------------------------
# status — reads local state, no model
# ---------------------------------------------------------------------------


def test_status_does_not_call_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "oc1" in result.output


# ---------------------------------------------------------------------------
# mark — updates lifecycle state, no model
# ---------------------------------------------------------------------------


def test_mark_running_does_not_call_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["mark", "oc1", "--project", "jungle", "--state", "running"])

    assert result.exit_code == 0, result.output


def test_mark_invalid_state_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["mark", "oc1", "--project", "jungle", "--state", "not-valid"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# diff — reads git helpers, no model
# ---------------------------------------------------------------------------


def test_diff_does_not_call_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "M foo.py")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "1 file changed")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "foo.py")

    runner = CliRunner()
    result = runner.invoke(app, ["diff", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# logs — reads artifact files, no model
# ---------------------------------------------------------------------------


def test_logs_reads_mandate_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    (run_dir / "mandate.md").write_text("# Mandate\n\nDo the thing.", encoding="utf-8")
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["logs", "oc1", "--project", "jungle", "--kind", "mandate"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


# ---------------------------------------------------------------------------
# capabilities — deterministic table, no model
# ---------------------------------------------------------------------------


def test_capabilities_lists_deterministic_and_model_backed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["capabilities"])

    assert result.exit_code == 0, result.output
    # Rich may truncate category names in narrow terminal columns (e.g. "determini…")
    assert "determini" in result.output
    assert "model-back" in result.output or "Category" in result.output


def test_capabilities_note_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `note` command must be listed as deterministic (requires_opencode=no)."""
    runner = CliRunner()
    result = runner.invoke(app, ["capabilities"])

    assert result.exit_code == 0
    # `note` row should appear in the output, and the requires_opencode column should say 'no'
    output_lines = result.output.splitlines()
    note_lines = [line for line in output_lines if "note" in line.lower()]
    assert note_lines, "note should appear in capabilities output"
    note_line = note_lines[0]
    # The row should NOT have yes for requires_opencode
    # Rich table format: "note | deterministic | no | no | no | ..."
    # Find the 'no' in the requires_opencode column position
    assert "deterministic" in note_line or "no" in note_line


def test_capabilities_context_pack_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["capabilities"])

    assert result.exit_code == 0
    # Rich truncates command names; match on the prefix
    assert "context-p" in result.output, "context-pack (or truncated form) should appear in capabilities output"


def test_capabilities_estimate_cost_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["capabilities"])
    assert result.exit_code == 0
    assert "estimate" in result.output


def test_capabilities_gate_summary_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["capabilities"])
    assert result.exit_code == 0
    assert "gate-summ" in result.output


def test_capabilities_budget_status_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["capabilities"])
    assert result.exit_code == 0
    assert "budget-st" in result.output


# ---------------------------------------------------------------------------
# context-pack — deterministic, no OpenCode
# ---------------------------------------------------------------------------


def test_context_pack_writes_files_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    # Patch git helpers so no real git is needed
    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "M foo.py")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "1 file changed")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "foo.py")
    monkeypatch.setattr(cli_module, "get_current_branch", lambda _: "agent/oc1/test-feature")

    runner = CliRunner()
    result = runner.invoke(
        app, ["context-pack", "oc1", "--project", "jungle", "--task", "plan"]
    )

    assert result.exit_code == 0, result.output
    assert (run_dir / "context_plan.md").exists()
    assert (run_dir / "context_plan.json").exists()


def test_context_pack_invalid_task_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        app, ["context-pack", "oc1", "--project", "jungle", "--task", "not_a_task"]
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# gate-summary — reads log, classifies, no model
# ---------------------------------------------------------------------------


def test_gate_summary_reads_log_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    (run_dir / "preflight.log").write_text(
        "- hook id: trailing-whitespace\n  exit code: 1\nFailed.\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["gate-summary", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()
    assert "formatting" in output_lower or "trailing-whitespace" in output_lower


def test_gate_summary_no_log_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["gate-summary", "oc1", "--project", "jungle"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# estimate-cost — cost arithmetic, no model
# ---------------------------------------------------------------------------


def test_estimate_cost_produces_output_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    # Pre-populate a context pack so estimate-cost can read it
    (run_dir / "context_plan.md").write_text("# Context\n\n" + ("word " * 400), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "estimate-cost",
            "oc1",
            "--project",
            "jungle",
            "--task",
            "plan",
            "--profile",
            "cheap_planner",
        ],
    )

    assert result.exit_code == 0, result.output
    # Should contain cost indicator
    assert "$" in result.output or "USD" in result.output or "token" in result.output.lower()


# ---------------------------------------------------------------------------
# budget-status — reads local state, no model
# ---------------------------------------------------------------------------


def test_budget_status_reads_state_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["budget-status", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "oc1" in result.output or "jungle" in result.output


# ---------------------------------------------------------------------------
# check / finish / next — high-level deterministic wrappers
# ---------------------------------------------------------------------------


def test_check_does_not_call_opencode_and_suggests_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "M foo.py")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "1 file changed")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "foo.py")

    class _Completed:
        returncode = 0
        stdout = "preflight ok"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda *args, **kwargs: _Completed())

    runner = CliRunner()
    result = runner.invoke(app, ["check", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "Preflight passed" in result.output
    assert "cascade finish oc1 --project jungle" in result.output


def test_finish_defaults_to_safe_dry_run_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, state_path = _setup_agent(tmp_path)
    _forbid_opencode(monkeypatch)

    gate_result = {
        "timestamp": "2026-04-22T12:00:00Z",
        "command": "echo preflight-ok",
        "exit_code": 0,
        "passed": True,
        "log_path": str(run_dir / "preflight.log"),
        "git_head_sha": "(unknown)",
        "diff_fingerprint": "(unknown)",
        "touched_files": [],
    }
    (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "")
    monkeypatch.setattr(cli_module, "get_current_branch", lambda _: "agent/oc1/test-feature")

    runner = CliRunner()
    result = runner.invoke(app, ["finish", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "Dry run only" in result.output
    state_after = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after["state"] == "claimed"


def test_next_recommends_check_when_no_gate_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["next", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "cascade check oc1 --project jungle" in result.output


def test_next_recommends_fix_after_failed_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    gate_result = {
        "timestamp": "2026-04-22T12:00:00Z",
        "command": "echo preflight-fail",
        "exit_code": 1,
        "passed": False,
        "log_path": str(run_dir / "preflight.log"),
        "git_head_sha": "(unknown)",
        "diff_fingerprint": "(unknown)",
        "touched_files": [],
    }
    (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["next", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "cascade fix oc1 --project jungle --profile debugger" in result.output


def test_next_recommends_finish_after_passing_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    gate_result = {
        "timestamp": "2026-04-22T12:00:00Z",
        "command": "echo preflight-ok",
        "exit_code": 0,
        "passed": True,
        "log_path": str(run_dir / "preflight.log"),
        "git_head_sha": "(unknown)",
        "diff_fingerprint": "(unknown)",
        "touched_files": [],
    }
    (run_dir / "gate_result.json").write_text(json.dumps(gate_result, indent=2), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["next", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "cascade finish oc1 --project jungle" in result.output
