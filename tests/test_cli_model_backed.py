"""Tests for model-backed commands: boundaries, profile resolution, no-launch guards."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import app
from cascade.config import ModelProfile
from cascade.opencode import OpenCodeMode, mode_to_agent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_project_with_profiles(tmp_path: Path) -> Path:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / "repo"}
  worktree_root: {tmp_path / "worktrees"}
commands:
  create_worktree: echo create
  preflight: echo preflight-ok
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
    input_cost_per_million: 0.06
    output_cost_per_million: 0.40
  profiles:
    cheap_planner:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      input_cost_per_million: 0.06
      output_cost_per_million: 0.40
      use_for:
        - plan
    executor:
      provider: openrouter
      model: z-ai/glm-4.7
      input_cost_per_million: 0.38
      output_cost_per_million: 1.74
      use_for:
        - implement
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def _setup_agent(tmp_path: Path, project: str = "jungle", agent: str = "oc1") -> tuple[Path, Path, Path]:
    worktree = tmp_path / "worktrees" / f"{agent}-test-feature"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)

    (run_dir / "mandate.md").write_text("# Mandate\n\nDo the thing.", encoding="utf-8")
    (run_dir / "launch_prompt.md").write_text("# Launch Prompt\n\nStart here.", encoding="utf-8")
    (run_dir / "decisions.md").write_text("# Decisions\n\n", encoding="utf-8")
    (run_dir / "questions.md").write_text("# Questions\n\n", encoding="utf-8")
    (run_dir / "running_summary.md").write_text("# Summary\n\nStarted.", encoding="utf-8")
    (run_dir / "transcript.md").write_text("# Transcript\n\n", encoding="utf-8")

    project_file = _write_project_with_profiles(tmp_path)
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


# ---------------------------------------------------------------------------
# OpenCode mode helpers (unit tests — no CLI needed)
# ---------------------------------------------------------------------------


def test_mode_to_agent_plan() -> None:
    assert mode_to_agent(OpenCodeMode.plan) == "plan"


def test_mode_to_agent_build() -> None:
    assert mode_to_agent(OpenCodeMode.build) == "build"


def test_mode_to_agent_none() -> None:
    assert mode_to_agent(None) is None


def test_invalid_mode_for_chat_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["chat", "oc1", "--project", "jungle", "--mode", "invalid"])
    assert result.exit_code != 0
    assert "invalid" in result.output


# ---------------------------------------------------------------------------
# run-agent: fails cleanly when OpenCode is missing
# ---------------------------------------------------------------------------


def test_run_agent_fails_cleanly_when_opencode_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)

    from cascade.opencode import OpenCodeError

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(OpenCodeError("opencode not found on PATH")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle"])

    assert result.exit_code != 0
    assert "opencode" in result.output.lower() or result.exit_code == 1


# ---------------------------------------------------------------------------
# chat: requires OpenCode — fails cleanly when missing
# ---------------------------------------------------------------------------


def test_chat_fails_cleanly_when_opencode_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    from cascade.opencode import OpenCodeError

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(OpenCodeError("opencode not found on PATH")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["chat", "oc1", "--project", "jungle"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ask: model-backed, fails if no transcript or OpenCode
# ---------------------------------------------------------------------------


def test_ask_fails_when_opencode_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    from cascade.opencode import OpenCodeError

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(OpenCodeError("opencode not found")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["ask", "oc1", "--project", "jungle", "--question", "What is the best approach?"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# claim: uses configured create_worktree — not hardcoded git worktree add
# ---------------------------------------------------------------------------


def test_claim_uses_configured_create_worktree_not_hardcoded_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = _write_project_with_profiles(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)

    shell_cmds: list[str] = []

    def _mock_run_command(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        shell_cmds.append(cmd)
        class _FakeResult:
            stdout = ""
        return _FakeResult()

    monkeypatch.setattr(cli_module, "run_command", _mock_run_command)
    monkeypatch.setattr(
        cli_module, "fetch_issue",
        lambda owner, repo, issue: {"title": "Test Issue", "body": "Body", "number": issue},
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["claim", "--project-file", str(project_file), "--issue", "45", "--agent", "oc1",
         "--model", "openrouter/z-ai/glm-4.7-flash"],
    )

    assert result.exit_code == 0, result.output
    # The create_worktree command should have been called
    assert any("echo" in cmd for cmd in shell_cmds)
    # It must NOT have called hardcoded 'git worktree add'
    for cmd in shell_cmds:
        if "git worktree add" in cmd:
            pytest.fail(
                f"claim used hardcoded 'git worktree add' instead of configured command. Got: {cmd!r}"
            )


# ---------------------------------------------------------------------------
# claim: launch prompt includes instruction files and worktree path
# ---------------------------------------------------------------------------


def test_claim_launch_prompt_includes_instruction_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / "repo"}
  worktree_root: {tmp_path / "worktrees"}
commands:
  create_worktree: echo create
instructions:
  files:
    - COPILOT.md
    - .github/copilot-instructions.md
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        cli_module, "fetch_issue",
        lambda owner, repo, issue: {"title": "Test Issue", "body": "Body.", "number": issue},
    )
    monkeypatch.setattr(
        cli_module, "run_command",
        lambda cmd, cwd=None: type("R", (), {"stdout": ""})(),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["claim", "--project-file", str(project_file), "--issue", "45",
         "--agent", "oc1", "--model", "openrouter/z-ai/glm-4.7-flash"],
    )

    assert result.exit_code == 0, result.output

    # Read the saved launch prompt
    run_dir = tmp_path / "state" / "jungle" / "runs" / "oc1"
    prompt = (run_dir / "launch_prompt.md").read_text(encoding="utf-8")

    assert "COPILOT.md" in prompt
    assert "copilot-instructions.md" in prompt


# ---------------------------------------------------------------------------
# prepare-model-call: builds prompt and metadata without OpenCode
# ---------------------------------------------------------------------------


def test_prepare_model_call_writes_files_without_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("prepare-model-call must not check for OpenCode")),
    )
    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "M foo.py")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "1 file changed")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "foo.py")
    monkeypatch.setattr(cli_module, "get_current_branch", lambda _: "agent/oc1/test-feature")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["prepare-model-call", "oc1", "--project", "jungle", "--task", "plan", "--profile", "cheap_planner"],
    )

    assert result.exit_code == 0, result.output
    assert (run_dir / "plan_prompt.md").exists()
    assert (run_dir / "plan_model_call.json").exists()

    meta = json.loads((run_dir / "plan_model_call.json").read_text(encoding="utf-8"))
    assert meta["task_type"] == "plan"
    assert meta["profile"] == "cheap_planner"
    assert "model_id" in meta
    assert "estimated_cost_usd" in meta


def test_prepare_model_call_model_id_uses_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "")
    monkeypatch.setattr(cli_module, "get_current_branch", lambda _: "agent/oc1/test-feature")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["prepare-model-call", "oc1", "--project", "jungle", "--task", "implement", "--profile", "executor"],
    )

    assert result.exit_code == 0, result.output
    meta = json.loads((run_dir / "implement_model_call.json").read_text(encoding="utf-8"))
    # Executor profile uses model z-ai/glm-4.7 under openrouter
    assert "glm-4.7" in meta["model_id"]
    assert "openrouter" in meta["model_id"]


def test_prepare_model_call_unknown_profile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["prepare-model-call", "oc1", "--project", "jungle", "--task", "plan", "--profile", "nonexistent_profile"],
    )

    assert result.exit_code != 0
