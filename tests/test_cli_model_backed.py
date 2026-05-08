"""Tests for model-backed commands: boundaries, profile resolution, no-launch guards."""
from __future__ import annotations

import json
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import app, resolve_prompt_path
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


def test_run_agent_uses_launch_prompt_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = ""

    def _mock_subprocess_run(cmd, cwd=None, check=False, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _Completed()

    monkeypatch.setattr(cli_module.subprocess, "run", _mock_subprocess_run)

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    cmd = captured["cmd"]
    assert cmd[:4] == ["opencode", ".", "--model", "openrouter/z-ai/glm-4.7-flash"]
    assert "--prompt" in cmd
    prompt_index = cmd.index("--prompt") + 1
    assert cmd[prompt_index] == (run_dir / "launch_prompt.md").read_text(encoding="utf-8")
    assert captured["cwd"] == worktree


def test_resolve_prompt_path_defaults_to_launch_prompt(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    launch_prompt = run_dir / "launch_prompt.md"
    launch_prompt.write_text("# Launch Prompt\n", encoding="utf-8")

    resolved = resolve_prompt_path(run_dir)

    assert resolved == launch_prompt


def test_resolve_prompt_path_uses_task_specific_prompt(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    task_prompt = run_dir / "implement_prompt.md"
    task_prompt.write_text("# Implement Prompt\n", encoding="utf-8")

    resolved = resolve_prompt_path(run_dir, task="implement")

    assert resolved == task_prompt


def test_resolve_prompt_path_rejects_invalid_prompt_file_type(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "launch_prompt.md").write_text("# Launch Prompt\n", encoding="utf-8")

    with pytest.raises(TypeError, match="prompt_file must be pathlib.Path"):
        resolve_prompt_path(run_dir, prompt_file=False)  # type: ignore[arg-type]


def test_run_agent_can_use_task_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    (run_dir / "implement_prompt.md").write_text("# Implement Prompt\n\nUse batching.", encoding="utf-8")

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = ""

    def _mock_subprocess_run(cmd, cwd=None, check=False, **kwargs):
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr(cli_module.subprocess, "run", _mock_subprocess_run)

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle", "--task", "implement"])

    assert result.exit_code == 0, result.output
    cmd = captured["cmd"]
    assert cmd[cmd.index("--prompt") + 1] == "# Implement Prompt\n\nUse batching."


def test_run_agent_with_prompt_true_uses_prompt_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir, _state = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = ""

    def _mock_subprocess_run(cmd, cwd=None, check=False, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _Completed()

    monkeypatch.setattr(cli_module.subprocess, "run", _mock_subprocess_run)

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle", "--with-prompt"])

    assert result.exit_code == 0, result.output
    cmd = captured["cmd"]
    assert "--prompt" in cmd
    assert cmd[cmd.index("--prompt") + 1] == (run_dir / "launch_prompt.md").read_text(encoding="utf-8")
    assert captured["cwd"] == worktree


def test_run_agent_prompt_file_override_works_without_printing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)
    custom_prompt = tmp_path / "custom_prompt.md"
    custom_prompt.write_text("TOP-SECRET-PROMPT-CONTENT", encoding="utf-8")

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = ""

    def _mock_subprocess_run(cmd, cwd=None, check=False, **kwargs):
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr(cli_module.subprocess, "run", _mock_subprocess_run)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["run-agent", "oc1", "--project", "jungle", "--prompt-file", str(custom_prompt)],
    )

    assert result.exit_code == 0, result.output
    assert captured["cmd"][captured["cmd"].index("--prompt") + 1] == "TOP-SECRET-PROMPT-CONTENT"
    assert "TOP-SECRET-PROMPT-CONTENT" not in result.output


def test_run_agent_no_prompt_skips_prompt_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = ""

    def _mock_subprocess_run(cmd, cwd=None, check=False, **kwargs):
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr(cli_module.subprocess, "run", _mock_subprocess_run)

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle", "--no-prompt"])

    assert result.exit_code == 0, result.output
    assert "--prompt" not in captured["cmd"]


def test_run_agent_non_interactive_uses_prompt_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli_module,
        "run_prompt",
        lambda prompt, worktree, model, mode=None, use_continue=True: captured.update(
            {
                "prompt": prompt,
                "worktree": worktree,
                "model": model,
                "mode": mode,
                "use_continue": use_continue,
            }
        )
        or "non-interactive output",
    )
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: type("_Completed", (), {"returncode": 0, "stdout": ""})(),
    )
    monkeypatch.setattr(
        cli_module,
        "build_interactive_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("interactive command must not be built")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert captured["prompt"] == (run_dir / "launch_prompt.md").read_text(encoding="utf-8")
    assert captured["use_continue"] is False
    assert "non-interactive output" in result.output


def test_run_agent_missing_prompt_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["run-agent", "oc1", "--project", "jungle", "--task", "review"])

    assert result.exit_code != 0
    assert "Prompt file not found" in result.output


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


def test_claim_runs_configured_init_mandate_after_worktree_creation(
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
branches:
    active_branch: staging
commands:
    create_worktree: custom-create --agent={{agent}} --slug={{slug}}
    init_mandate: make mandate-start MANDATE_SLUG={{slug}} MANDATE_TITLE='{{title}}' MANDATE_ACTIVE_BRANCH={{active_branch_shell}}
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
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)

    commands: list[tuple[str, Path | None]] = []

    def _mock_run_command(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        commands.append((cmd, cwd))
        if "custom-create" in cmd:
            worktree = worktrees / "oc1-test-issue"
            (worktree / ".github" / "mandates").mkdir(parents=True, exist_ok=True)
        if "make mandate-start" in cmd:
            metadata = worktrees / "oc1-test-issue" / ".github" / "mandates" / "test-issue.json"
            metadata.write_text("{}\n", encoding="utf-8")

        class _FakeResult:
            stdout = ""

        return _FakeResult()

    monkeypatch.setattr(cli_module, "run_command", _mock_run_command)
    monkeypatch.setattr(
        cli_module,
        "fetch_issue",
        lambda owner, repo, issue: {"title": "Test Issue", "body": "Body", "number": issue},
    )
    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("claim must not check OpenCode")),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["claim", "--project-file", str(project_file), "--issue", "45", "--agent", "oc1", "--model", "openrouter/z-ai/glm-4.7-flash"],
    )

    assert result.exit_code == 0, result.output
    assert [cmd for cmd, _cwd in commands] == [
        "custom-create --agent=oc1 --slug=test-issue",
        "git status --porcelain",
        "make mandate-start MANDATE_SLUG=test-issue MANDATE_TITLE='Test Issue' MANDATE_ACTIVE_BRANCH=staging",
    ]
    assert commands[1][1] == worktrees / "oc1-test-issue"
    metadata_path = worktrees / "oc1-test-issue" / ".github" / "mandates" / "test-issue.json"
    assert metadata_path.exists()
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["status"] == "in_progress"
    assert payload["repo"] == "jungle"
    assert payload["agent_branch"] == "agent/oc1/test-issue"
    assert payload["active_branch"] == "staging"
    assert payload["worktree_path"] == str((worktrees / "oc1-test-issue").resolve())


def test_preflight_after_claim_does_not_fail_not_in_progress(
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
branches:
    active_branch: staging
commands:
    create_worktree: custom-create --agent={{agent}} --slug={{slug}}
    init_mandate: make mandate-start MANDATE_SLUG={{slug}} MANDATE_TITLE='{{title}}' MANDATE_ACTIVE_BRANCH={{active_branch_shell}}
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
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)

    def _mock_run_command(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        if "custom-create" in cmd:
            worktree = worktrees / "oc1-test-issue"
            (worktree / ".github" / "mandates").mkdir(parents=True, exist_ok=True)
        if "make mandate-start" in cmd:
            metadata = worktrees / "oc1-test-issue" / ".github" / "mandates" / "test-issue.json"
            metadata.write_text(
                json.dumps(
                    {
                        "slug": "test-issue",
                        "status": "done",
                        "repo": "wrong-worktree-name",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        class _FakeResult:
            stdout = ""

        return _FakeResult()

    monkeypatch.setattr(cli_module, "run_command", _mock_run_command)
    monkeypatch.setattr(
        cli_module,
        "fetch_issue",
        lambda owner, repo, issue: {"title": "Test Issue", "body": "Body", "number": issue},
    )

    def _mock_preflight_runner(*, command: str, worktree: Path, log_path: Path, verbose: bool, watch: bool):
        metadata = worktree / ".github" / "mandates" / "test-issue.json"
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        if payload.get("status") != "in_progress":
            return cli_module.PreflightRunResult(
                returncode=1,
                output="[mandate] ERROR: Mandate test-issue is not in progress",
            )
        return cli_module.PreflightRunResult(returncode=0, output="ok")

    monkeypatch.setattr(cli_module, "_run_preflight_command", _mock_preflight_runner)

    runner = CliRunner()
    claim_result = runner.invoke(
        app,
        ["claim", "--project-file", str(project_file), "--issue", "45", "--agent", "oc1", "--model", "openrouter/z-ai/glm-4.7-flash"],
    )
    assert claim_result.exit_code == 0, claim_result.output

    preflight_result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])
    assert preflight_result.exit_code == 0, preflight_result.output
    assert "not in progress" not in preflight_result.output.lower()


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


# ---------------------------------------------------------------------------
# start: high-level orchestration command
# ---------------------------------------------------------------------------


def test_start_no_launch_claims_and_prepares_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = _write_project_with_profiles(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        cli_module,
        "fetch_issue",
        lambda owner, repo, issue: {"title": "Start Command Issue", "body": "Body", "number": issue},
    )

    def _mock_run_command(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        # Simulate the configured create_worktree command creating the expected path.
        if "echo create" in cmd:
            (tmp_path / "worktrees" / "oc1-start-command-issue").mkdir(parents=True, exist_ok=True)

        class _FakeResult:
            stdout = ""

        return _FakeResult()

    monkeypatch.setattr(cli_module, "run_command", _mock_run_command)
    monkeypatch.setattr(
        cli_module,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run_agent must not be called with --no-launch")),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "start",
            "45",
            "--agent",
            "oc1",
            "--project-file",
            str(project_file),
            "--profile",
            "executor",
            "--no-launch",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Start complete (no launch)" in result.output
    assert "Next: cascade run-agent oc1 --project jungle" in result.output
    assert (tmp_path / "state" / "jungle" / "agents" / "oc1.json").exists()
    assert (tmp_path / "state" / "jungle" / "runs" / "oc1" / "context_implement.md").exists()


def test_start_launch_calls_run_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = _write_project_with_profiles(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        cli_module,
        "fetch_issue",
        lambda owner, repo, issue: {"title": "Start Command Issue", "body": "Body", "number": issue},
    )

    def _mock_run_command(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        # Simulate the configured create_worktree command creating the expected path.
        if "echo create" in cmd:
            (tmp_path / "worktrees" / "oc1-start-command-issue").mkdir(parents=True, exist_ok=True)

        class _FakeResult:
            stdout = ""

        return _FakeResult()

    monkeypatch.setattr(cli_module, "run_command", _mock_run_command)

    called: dict[str, object] = {}

    def _mock_run_agent(agent: str, project: str, print_prompt: bool = False, **kwargs) -> None:
        called["agent"] = agent
        called["project"] = project
        called["print_prompt"] = print_prompt
        called.update(kwargs)

    monkeypatch.setattr(cli_module, "run_agent", _mock_run_agent)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "start",
            "45",
            "--agent",
            "oc1",
            "--project-file",
            str(project_file),
            "--profile",
            "executor",
        ],
    )

    assert result.exit_code == 0, result.output
    assert called == {"agent": "oc1", "project": "jungle", "print_prompt": False, "task": "implement"}


def test_start_auto_launch_failure_still_prints_manual_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = _write_project_with_profiles(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        cli_module,
        "fetch_issue",
        lambda owner, repo, issue: {"title": "Start Command Issue", "body": "Body", "number": issue},
    )

    def _mock_run_command(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        if "echo create" in cmd:
            (tmp_path / "worktrees" / "oc1-start-command-issue").mkdir(parents=True, exist_ok=True)

        class _FakeResult:
            stdout = ""

        return _FakeResult()

    monkeypatch.setattr(cli_module, "run_command", _mock_run_command)
    monkeypatch.setattr(
        cli_module,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("Resolved prompt path has invalid exists attribute")),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "start",
            "45",
            "--agent",
            "oc1",
            "--project-file",
            str(project_file),
            "--profile",
            "executor",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Auto-launch failed after setup" in result.output
    assert "Worktree:" in result.output
    assert "Prompt file:" in result.output
    assert "Manual launch: cascade run-agent oc1 --project jungle" in result.output
    assert "Loop alternative: cascade loop oc1 --project jungle --watch" in result.output


def test_continue_uses_continue_prompt_when_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)

    called: dict[str, object] = {}

    def _mock_run_agent(agent: str, project: str, print_prompt: bool = False, **kwargs) -> None:
        called["agent"] = agent
        called["project"] = project
        called["print_prompt"] = print_prompt
        called.update(kwargs)

    monkeypatch.setattr(cli_module, "run_agent", _mock_run_agent)

    runner = CliRunner()
    result = runner.invoke(app, ["continue", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert (run_dir / "continue_prompt.md").exists()
    assert called == {"agent": "oc1", "project": "jungle", "print_prompt": False, "task": "continue", "mode": None}


def test_fix_no_launch_does_not_call_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir, _state = _setup_agent(tmp_path)
    (run_dir / "preflight.log").write_text("FAILED: mypy\n", encoding="utf-8")
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

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("fix --no-launch must not check for OpenCode")),
    )
    monkeypatch.setattr(cli_module, "get_git_status", lambda _: "")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda _: "")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda _: "")
    monkeypatch.setattr(cli_module, "get_current_branch", lambda _: "agent/oc1/test-feature")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["fix", "oc1", "--project", "jungle", "--profile", "executor", "--no-launch"],
    )

    assert result.exit_code == 0, result.output
    assert "Fix context prepared (no launch)" in result.output
    assert (run_dir / "fix_prompt.md").exists()
