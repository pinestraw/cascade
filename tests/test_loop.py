from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import LoopRunOptions, app, run_auto_repair_loop
from cascade.gates import save_gate_result
from cascade.opencode import OpenCodeRunResult


@pytest.fixture(autouse=True)
def _mock_non_interactive_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))


def _write_project_file(
    tmp_path: Path,
    *,
    gate_fixes_block: str = "",
    repair_routing_block: str = "",
    closeout_dirty_file_command: str | None = None,
) -> Path:
    closeout_block = f"  closeout_dirty_file: {closeout_dirty_file_command}\n" if closeout_dirty_file_command else ""
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
{closeout_block}repair_loop:
    max_iterations: 3
    max_model_fixes: 2
    max_estimated_cost_usd: 2.5
    stop_on_same_failure_twice: true
    require_approval_categories:
        - security
        - policy
        - migration
    forbidden_touched_file_patterns:
        - .pre-commit-config.yaml
{gate_fixes_block}
{repair_routing_block}
models:
    default:
        provider: openrouter
        model: z-ai/glm-4.7-flash
    profiles:
        cheap_coder:
            provider: openrouter
            model: z-ai/glm-4.7-flash
            input_cost_per_million: 0.06
            output_cost_per_million: 0.40
        debugger:
            provider: openrouter
            model: deepseek/deepseek-v3.2
            input_cost_per_million: 0.25
            output_cost_per_million: 0.40
        executor:
            provider: openrouter
            model: z-ai/glm-4.7
            input_cost_per_million: 0.38
            output_cost_per_million: 1.74
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def _setup_agent(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktrees" / "a1-loop-test"
    worktree.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mandate.md").write_text("# Mandate\n\nLoop test", encoding="utf-8")

    project_file = _write_project_file(tmp_path)

    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 45,
        "title": "Loop Test",
        "slug": "loop-test",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }
    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return project_file, worktree, run_dir


def _setup_workspace_link_agent(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-secrets" / "instica").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-secrets" / "instica" / ".env.local").write_text("TOKEN=1\n", encoding="utf-8")

    worktree = tmp_path / "worktrees" / "a1-loop-test"
    worktree.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mandate.md").write_text("# Mandate\n\nLoop test", encoding="utf-8")

    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: {tmp_path}
  repo_root: {tmp_path / 'repo'}
  worktree_root: {tmp_path / 'worktrees'}
  secrets_root: {tmp_path / 'jungle-secrets'}
workspace_links:
  - link: "{{worktree_root}}/jungle-secrets"
    target: "{{secrets_root}}"
commands:
  create_worktree: echo create
  preflight: make mandate-preflight MANDATE_SLUG={{slug}}
repair_loop:
  max_iterations: 5
  max_model_fixes: 2
  max_estimated_cost_usd: 2.5
  stop_on_same_failure_twice: true
  require_approval_categories:
    - security
    - policy
    - migration
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 45,
        "title": "Loop Test",
        "slug": "loop-test",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }
    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return project_file, worktree, run_dir


def _options(**kwargs: Any) -> LoopRunOptions:
    defaults: dict[str, Any] = {
        "max_iterations": 3,
        "max_model_fixes": 2,
        "max_estimated_cost_usd": 2.5,
        "dry_run": False,
        "non_interactive": None,
        "verbose": False,
        "watch": False,
        "profile": None,
        "cheap_profile": "cheap_coder",
        "debug_profile": "debugger",
        "executor_profile": "executor",
        "stop_on_same_failure_twice": True,
    }
    defaults.update(kwargs)
    return LoopRunOptions(**defaults)


def _preflight_factory(run_dir: Path, outcomes: list[tuple[bool, str, list[str]]]):
    idx = {"i": 0}

    def _preflight(agent: str, project: str) -> None:
        i = min(idx["i"], len(outcomes) - 1)
        idx["i"] += 1
        passed, log_text, touched = outcomes[i]
        log_path = run_dir / "preflight.log"
        log_path.write_text(log_text, encoding="utf-8")
        gate_data: dict[str, object] = {
            "timestamp": "2026-04-22T12:00:00Z",
            "command": "make mandate-preflight MANDATE_SLUG=loop-test",
            "exit_code": 0 if passed else 1,
            "passed": passed,
            "log_path": str(log_path),
            "git_head_sha": "deadbeef",
            "diff_fingerprint": "abc123",
            "touched_files": touched,
        }
        save_gate_result(run_dir, gate_data)
        if not passed:
            raise typer.Exit(1)

    return _preflight


def test_loop_stops_immediately_when_preflight_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(True, "ok", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(), active_branch=None)

    assert result["status"] == "passed"
    assert result["iterations"] == 1


def test_loop_runs_deterministic_autofix_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    gate_fixes_block = """
gate_fixes:
  trailing-whitespace:
    command: pre-commit run trailing-whitespace --all-files
    model_required: false
"""
    project_file = _write_project_file(tmp_path, gate_fixes_block=gate_fixes_block)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktrees" / "a1-loop-test"
    worktree.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mandate.md").write_text("# Mandate\n", encoding="utf-8")
    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 45,
        "title": "Loop Test",
        "slug": "loop-test",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }
    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    monkeypatch.setattr(
        cli_module,
        "preflight",
        _preflight_factory(
            run_dir,
            [
                (False, "Failed: trailing-whitespace", ["foo.py"]),
                (True, "ok", ["foo.py"]),
            ],
        ),
    )

    commands: list[str] = []

    def _run_command(cmd: str, cwd: Path | None = None):
        commands.append(cmd)

        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run_command)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: ["foo.py"])

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(), active_branch=None)

    assert result["status"] == "passed"
    assert any("trailing-whitespace" in cmd for cmd in commands)


def test_loop_does_not_run_unconfigured_deterministic_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: trailing-whitespace", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    model_attempts = {"count": 0}

    def _model_fix(**kwargs: Any):
        model_attempts["count"] += 1
        return True, "model fix", 0.1, "debugger"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_model_fixes=0), active_branch=None)

    assert result["status"] == "stopped"
    assert result["last_action"] == "deterministic_fix_not_configured"
    assert model_attempts["count"] == 0


def test_loop_prepares_model_fix_for_typing_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    seen = {"called": False}

    def _model_fix(**kwargs: Any):
        seen["called"] = True
        return True, "model fix", 0.1, "debugger"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=1), active_branch=None)

    assert result["status"] == "stopped"
    assert seen["called"] is True


def test_loop_respects_max_iterations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model", 0.0, "debugger"))

    result = run_auto_repair_loop(
        project="jungle",
        agent="a1",
        options=_options(max_iterations=2, stop_on_same_failure_twice=False),
        active_branch=None,
    )

    assert result["status"] == "stopped"
    assert result["last_action"] == "max_iterations_reached"
    assert result["iterations"] == 2


def test_loop_respects_max_model_fixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model", 0.1, "debugger"))

    result = run_auto_repair_loop(
        project="jungle",
        agent="a1",
        options=_options(max_model_fixes=1, max_iterations=3, stop_on_same_failure_twice=False),
        active_branch=None,
    )

    assert result["status"] == "stopped"
    assert result["last_action"] == "max_model_fixes_reached"


def test_loop_respects_max_estimated_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model", 2.0, "debugger"))

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_estimated_cost_usd=1.0), active_branch=None)

    assert result["status"] == "stopped"
    assert result["last_action"] == "budget_exceeded"


def test_loop_stops_on_repeated_failure_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "preflight",
        _preflight_factory(
            run_dir,
            [
                (False, "Failed: mypy\nerror A", ["a.py"]),
                (False, "Failed: mypy\nerror A", ["a.py"]),
            ],
        ),
    )
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: ["a.py"])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model", 0.1, "debugger"))

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=3), active_branch=None)

    assert result["status"] == "stopped"
    assert result["last_action"] == "same_failure_repeated"


def test_security_and_policy_categories_require_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: bandit", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(), active_branch=None)

    assert result["status"] == "needs_human"
    assert result["last_action"] == "approval_required"


def test_model_fix_prompt_is_bounded_and_has_safety_instructions() -> None:
    prompt = cli_module._build_loop_fix_prompt(
        category="typing",
        hook="mypy",
        log_tail="error: name not defined",
        touched_files=["a.py"],
        diff_stat="1 file changed",
    )

    assert "Fix only this specific failure" in prompt
    assert "Do not weaken, bypass, disable" in prompt
    assert "Do not stage, commit, or push" in prompt


def test_loop_metadata_json_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(True, "ok", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    run_auto_repair_loop(project="jungle", agent="a1", options=_options(), active_branch=None)

    metadata_path = run_dir / "repair_loop.json"
    assert metadata_path.exists()
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"


def test_check_auto_fix_calls_same_loop_logic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    called: dict[str, Any] = {"count": 0, "options": None}

    def _loop_runner(**kwargs: Any):
        called["count"] += 1
        called["options"] = kwargs["options"]
        return {"status": "passed", "iterations": 1, "model_fix_attempts": 0, "estimated_cost_spent": 0.0}

    monkeypatch.setattr(cli_module, "run_auto_repair_loop", _loop_runner)

    runner = CliRunner()
    result = runner.invoke(app, ["check", "a1", "--project", "jungle", "--auto-fix"])

    assert result.exit_code == 0, result.output
    assert called["count"] == 1
    options = called["options"]
    assert isinstance(options, LoopRunOptions)
    assert options.max_iterations == 2
    assert options.max_model_fixes == 1
    assert options.max_estimated_cost_usd == pytest.approx(1.0)


def test_loop_cli_accepts_non_interactive_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    captured: dict[str, Any] = {"options": None}

    def _loop_runner(**kwargs: Any):
        captured["options"] = kwargs["options"]
        return {
            "status": "stopped",
            "iterations": 1,
            "model_fix_attempts": 0,
            "estimated_cost_spent": 0.0,
            "stop_reason": "done",
        }

    monkeypatch.setattr(cli_module, "run_auto_repair_loop", _loop_runner)

    runner = CliRunner()
    result = runner.invoke(app, ["loop", "a1", "--project", "jungle", "--non-interactive"])

    assert result.exit_code != 0
    assert isinstance(captured["options"], LoopRunOptions)
    assert captured["options"].non_interactive is True


def test_loop_cli_accepts_watch_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    captured: dict[str, Any] = {"options": None}

    def _loop_runner(**kwargs: Any):
        captured["options"] = kwargs["options"]
        return {
            "status": "stopped",
            "iterations": 1,
            "model_fix_attempts": 0,
            "estimated_cost_spent": 0.0,
            "stop_reason": "done",
        }

    monkeypatch.setattr(cli_module, "run_auto_repair_loop", _loop_runner)

    runner = CliRunner()
    result = runner.invoke(app, ["loop", "a1", "--project", "jungle", "--watch"])

    assert result.exit_code != 0
    assert isinstance(captured["options"], LoopRunOptions)
    assert captured["options"].watch is True


def test_loop_cli_accepts_verbose_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    captured: dict[str, Any] = {"options": None}

    def _loop_runner(**kwargs: Any):
        captured["options"] = kwargs["options"]
        return {
            "status": "stopped",
            "iterations": 1,
            "model_fix_attempts": 0,
            "estimated_cost_spent": 0.0,
            "stop_reason": "done",
        }

    monkeypatch.setattr(cli_module, "run_auto_repair_loop", _loop_runner)

    runner = CliRunner()
    result = runner.invoke(app, ["loop", "a1", "--project", "jungle", "--verbose"])

    assert result.exit_code != 0
    assert isinstance(captured["options"], LoopRunOptions)
    assert captured["options"].verbose is True


def test_forbidden_file_touches_stop_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", [])]))

    touched = iter([
        ["src/app.py"],
        ["src/app.py", ".pre-commit-config.yaml"],
    ])
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: next(touched))
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model", 0.1, "debugger"))

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=1), active_branch=None)

    assert result["status"] == "needs_human"
    assert result["last_action"] == "forbidden_files_touched"


def test_loop_rechecks_after_model_attempt_until_preflight_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str) -> None:
        preflight_calls["count"] += 1
        if preflight_calls["count"] == 1:
            log_path = run_dir / "preflight.log"
            log_path.write_text("Failed: mypy\nerror: first pass fails", encoding="utf-8")
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": ["a.py"],
                },
            )
            raise typer.Exit(1)
        log_path = run_dir / "preflight.log"
        log_path.write_text("ok", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc124",
                "touched_files": ["a.py"],
            },
        )

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: ["a.py"])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model completed", 0.1, "debugger"))

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=3), active_branch=None)

    assert result["status"] == "passed"
    assert preflight_calls["count"] == 2


def test_loop_non_interactive_uses_opencode_run_and_reruns_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, run_dir = _setup_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False) -> None:
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        if preflight_calls["count"] == 1:
            log_path.write_text("Failed: mypy\nerror: first pass fails", encoding="utf-8")
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": ["a.py"],
                },
            )
            raise typer.Exit(1)
        log_path.write_text("ok", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc124",
                "touched_files": ["a.py"],
            },
        )

    seen: dict[str, object] = {}

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    monkeypatch.setattr(
        cli_module,
        "run_prompt_with_result",
        lambda prompt, worktree, model, mode, use_continue: seen.update(
            {
                "prompt": prompt,
                "worktree": worktree,
                "model": model,
                "mode": mode,
                "use_continue": use_continue,
            }
        )
        or OpenCodeRunResult(
            command=["opencode", "run", "--model", str(model), str(prompt)],
            returncode=0,
            stdout="fixed",
            stderr="",
        ),
    )

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=3), active_branch=None)

    assert result["status"] == "passed"
    assert preflight_calls["count"] == 2
    assert seen["worktree"] == worktree
    assert seen["use_continue"] is True


def test_loop_watch_streams_output_and_saves_log_and_reruns_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False) -> None:
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        if preflight_calls["count"] == 1:
            log_path.write_text("Failed: mypy\nerror: first pass fails", encoding="utf-8")
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": ["a.py"],
                },
            )
            raise typer.Exit(1)
        log_path.write_text("ok", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc124",
                "touched_files": ["a.py"],
            },
        )

    def _run_prompt_streaming(**kwargs: Any) -> OpenCodeRunResult:
        on_line = kwargs["on_line"]
        on_line("first line")
        on_line("second line")
        log_path = kwargs["log_path"]
        assert isinstance(log_path, Path)
        log_path.write_text("first line\nsecond line\n", encoding="utf-8")
        return OpenCodeRunResult(
            command=["opencode", "run"],
            returncode=0,
            stdout="first line\nsecond line\n",
            stderr="",
        )

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    monkeypatch.setattr(cli_module, "run_prompt_streaming", _run_prompt_streaming)

    result = run_auto_repair_loop(
        project="jungle",
        agent="a1",
        options=_options(max_iterations=3, watch=True),
        active_branch=None,
    )

    output = capsys.readouterr().out
    assert result["status"] == "passed"
    assert preflight_calls["count"] == 2
    assert "[opencode] first line" in output
    assert "[loop] rerunning preflight" in output
    log_path = run_dir / "loop_opencode_1.log"
    assert log_path.exists()
    assert "first line" in log_path.read_text(encoding="utf-8")


def test_loop_forwards_watch_to_preflight_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    seen_calls: list[tuple[bool, bool]] = []
    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False) -> None:
        seen_calls.append((verbose, watch))
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        if preflight_calls["count"] == 1:
            log_path.write_text("Failed: mypy\n", encoding="utf-8")
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": ["a.py"],
                },
            )
            raise typer.Exit(1)
        log_path.write_text("ok", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc124",
                "touched_files": ["a.py"],
            },
        )

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model completed", 0.1, "debugger"))

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=3, watch=True), active_branch=None)

    assert result["status"] == "passed"
    assert seen_calls == [(False, True), (False, True)]


def test_loop_default_mode_is_automation_safe_when_non_interactive_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "supports_non_interactive_run",
        lambda: (False, "opencode run unavailable"),
    )
    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", ["a.py"])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    monkeypatch.setattr(
        cli_module,
        "run_agent",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("default loop must not launch interactive TUI")),
    )

    with pytest.raises(cli_module.OpenCodeError, match="automation-safe"):
        run_auto_repair_loop(
            project="jungle",
            agent="a1",
            options=_options(max_iterations=3, non_interactive=None),
            active_branch=None,
        )


def test_loop_explicit_interactive_mode_warns_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "preflight",
        _preflight_factory(
            run_dir,
            [
                (False, "Failed: mypy", ["a.py"]),
                (True, "ok", ["a.py"]),
            ],
        ),
    )
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    called = {"value": False}

    def _run_agent(**kwargs: Any) -> None:
        called["value"] = True

    monkeypatch.setattr(cli_module, "run_agent", _run_agent)

    result = run_auto_repair_loop(
        project="jungle",
        agent="a1",
        options=_options(max_iterations=3, non_interactive=False),
        active_branch=None,
    )

    captured = capsys.readouterr().out
    assert result["status"] == "passed"
    assert called["value"] is True
    assert "Interactive OpenCode will not auto-exit" in captured


def test_loop_stops_when_model_switches_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", ["a.py"])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", lambda **kwargs: (True, "model", 0.1, "debugger"))

    branches = iter([
        "agent/a1/loop-test",
        "agent/a1/loop-test",
        "agent/a1/renamed",
        "agent/a1/renamed",
    ])
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: next(branches, "agent/a1/renamed"))

    result = run_auto_repair_loop(
        project="jungle",
        agent="a1",
        options=_options(max_iterations=2, stop_on_same_failure_twice=False),
        active_branch=None,
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "model-branch-switch"


def test_loop_stops_when_precommit_failures_reaches_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, worktree, run_dir = _setup_agent(tmp_path)
    mandates_dir = worktree / ".github" / "mandates"
    mandates_dir.mkdir(parents=True, exist_ok=True)
    (mandates_dir / "loop-test.json").write_text(
        json.dumps({"precommit_failures": 3}),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", ["a.py"])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=2), active_branch=None)

    assert result["status"] == "needs_human"
    assert result["stop_reason"] == "precommit-failures-limit"


def test_loop_stops_with_open_code_exit_nonzero_and_saves_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: mypy", ["a.py"])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda wt: ["a.py"])
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    monkeypatch.setattr(
        cli_module,
        "run_prompt_with_result",
        lambda **kwargs: OpenCodeRunResult(
            command=["opencode", "run", "--model", "x", "prompt"],
            returncode=2,
            stdout="stdout payload",
            stderr="stderr payload",
        ),
    )

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=2), active_branch=None)

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "open_code_exit_nonzero"
    opencode_log = run_dir / "loop_opencode_1.log"
    assert opencode_log.exists()
    log_content = opencode_log.read_text(encoding="utf-8")
    assert "stdout payload" in log_content
    assert "stderr payload" in log_content


def test_model_output_never_counts_as_validation_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "preflight",
        _preflight_factory(
            run_dir,
            [
                (False, "Failed: mypy\nerror A", ["a.py"]),
                (False, "Failed: mypy\nerror B", ["a.py"]),
            ],
        ),
    )
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: ["a.py"])
    monkeypatch.setattr(
        cli_module,
        "_run_model_fix_attempt",
        lambda **kwargs: (True, "Model says fixed", 0.1, "debugger"),
    )

    result = run_auto_repair_loop(
        project="jungle",
        agent="a1",
        options=_options(max_iterations=2, stop_on_same_failure_twice=False),
        active_branch=None,
    )

    assert result["status"] == "stopped"
    assert result["last_action"] == "max_iterations_reached"


def test_unknown_failure_routes_to_debugger_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(False, "Failed: random-hook", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    seen_profile: dict[str, str] = {"value": ""}

    def _model_fix(**kwargs: Any):
        seen_profile["value"] = kwargs["options"].debug_profile
        return True, "model fix", 0.1, kwargs["options"].debug_profile

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)

    run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=1), active_branch=None)

    assert seen_profile["value"] == "debugger"


def test_missing_metadata_uses_repair_without_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file, worktree, run_dir = _setup_agent(tmp_path)

    agent_state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 45,
        "title": "Loop Test",
        "slug": "loop-test",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }

    finding = cli_module.RepairFinding(
        kind=cli_module.RepairKind.missing_mandate_metadata,
        slug="loop-test",
        title="Loop Test",
        worktree=worktree,
        metadata_path=worktree / ".github" / "mandates" / "loop-test.json",
        canonical_mandate_path=run_dir / "mandate.md",
        message="missing metadata",
        can_repair=True,
        repair_command="echo repair",
    )

    monkeypatch.setattr(cli_module, "load_agent_state", lambda project, agent: agent_state)
    monkeypatch.setattr(cli_module, "detect_missing_mandate_metadata", lambda *args, **kwargs: finding)
    monkeypatch.setattr(
        cli_module,
        "repair_missing_mandate_metadata",
        lambda *args, **kwargs: cli_module.RepairResult(
            kind=cli_module.RepairKind.missing_mandate_metadata,
            success=True,
            dry_run=False,
            message="repaired",
            log_path=run_dir / "repair.log",
        ),
    )
    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(True, "ok", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    model_called = {"value": False}

    def _model_fix(**kwargs: Any):
        model_called["value"] = True
        return True, "model", 0.1, "debugger"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(), active_branch=None)

    assert result["status"] == "passed"
    assert model_called["value"] is False


def test_loop_metadata_contains_lifecycle_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "preflight", _preflight_factory(run_dir, [(True, "ok", [])]))
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])

    run_auto_repair_loop(project="jungle", agent="a1", options=_options(), active_branch=None)

    payload = json.loads((run_dir / "repair_loop.json").read_text(encoding="utf-8"))
    assert payload["started_at"]
    assert payload["updated_at"]
    assert payload["max_iterations"] == 3
    assert payload["max_model_fixes"] == 2
    assert payload["max_estimated_cost"] == pytest.approx(2.5)
    assert isinstance(payload["preflight_log_paths"], list)
    assert "model_profiles_used" in payload


def test_loop_status_displays_latest_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    metadata = {
        "status": "stopped",
        "stop_reason": "Same failure signature repeated; manual review required.",
        "iterations": 2,
        "model_fixes_used": 1,
        "estimated_cost_used": 0.125,
        "last_failure_category": "typing",
        "last_failure_hook": "mypy",
        "last_dirty_file_path": "api/serializers/inventory.py",
        "last_action": "same_failure_repeated",
    }
    (run_dir / "repair_loop.json").write_text(json.dumps(metadata), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["loop-status", "a1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert "same_failure_repeated" in result.output
    assert "mypy" in result.output
    assert "api/serializers/inventory.py" in result.output


def test_dirty_closeout_stops_without_configured_repair_and_no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "preflight",
        _preflight_factory(
            run_dir,
            [
                (
                    False,
                    "ERROR: Unexpected dirty file while closing mandate: api/serializers/inventory.py",
                    ["api/serializers/inventory.py"],
                )
            ],
        ),
    )
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: ["api/serializers/inventory.py"])
    monkeypatch.setattr(
        cli_module,
        "prepare_mandate_closeout_dirty_files",
        lambda *args, **kwargs: cli_module.RepairResult(
            kind=cli_module.RepairKind.closeout_dirty_file_prep,
            success=True,
            dry_run=False,
            message="Mandate-owned dirty files and mandate metadata staged.",
            log_path=run_dir / "closeout_prep.log",
        ),
    )

    model_called = {"value": False}

    def _model_fix(**kwargs: Any):
        model_called["value"] = True
        return True, "model", 0.1, "debugger"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=3), active_branch=None)

    assert result["status"] == "needs_human"
    assert result["stop_reason"] == "dirty_file_commit_required"
    assert result["last_failure_hook"] == "mandate-dirty-file"
    assert result["last_dirty_file_path"] == "api/serializers/inventory.py"
    assert "closeout-prep" in str(result.get("next_command", ""))
    assert result["status"] != "running"
    assert model_called["value"] is False


def test_dirty_closeout_path_shown_in_loop_status_and_next_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    payload = {
        "status": "needs_human",
        "stop_reason": "dirty_file_commit_required",
        "iterations": 1,
        "model_fixes_used": 0,
        "estimated_cost_used": 0.0,
        "last_failure_category": "workflow",
        "last_failure_hook": "mandate-dirty-file",
        "last_dirty_file_path": "api/serializers/inventory.py",
        "last_action": "closeout_dirty_file_detected",
        "next_command": "cascade closeout-prep a1 --project jungle --stage --commit --yes",
    }
    (run_dir / "repair_loop.json").write_text(json.dumps(payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["loop-status", "a1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "needs_human" in result.output
    assert "api/serializers/inventory.py" in result.output
    assert "closeout-prep a1 --project jungle" in result.output


def test_mandate_metadata_dirty_stops_without_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "preflight",
        _preflight_factory(
            run_dir,
            [
                (
                    False,
                    "M .github/mandates/audit.log\n?? .github/mandates/enrich-audit-log-messages.json",
                    [".github/mandates/audit.log", ".github/mandates/enrich-audit-log-messages.json"],
                )
            ],
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "get_touched_files",
        lambda worktree: [".github/mandates/audit.log", ".github/mandates/enrich-audit-log-messages.json"],
    )

    model_called = {"value": False}

    def _model_fix(**kwargs: Any):
        model_called["value"] = True
        return True, "model", 0.1, "debugger"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=2), active_branch=None)

    assert result["status"] == "needs_human"
    assert result["stop_reason"] == "mandate_metadata_requires_closeout_action"
    assert result["last_failure_hook"] == "mandate-metadata"
    assert model_called["value"] is False
    payload = json.loads((run_dir / "repair_loop.json").read_text(encoding="utf-8"))
    assert payload["status"] != "running"
    assert payload["stop_reason"]


def test_loop_status_rewrites_stale_running_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    payload = {
        "status": "running",
        "stop_reason": None,
        "iterations": 1,
        "last_action": "run_preflight",
    }
    metadata_path = run_dir / "repair_loop.json"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["loop-status", "a1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    updated = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert updated["status"] == "stopped"
    assert updated["stop_reason"] == "loop_process_not_active"
    assert "stopped" in result.output
    assert "loop_process_not_active" in result.output


def test_loop_repairs_missing_workspace_link_and_reruns_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_workspace_link_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str) -> None:
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        if preflight_calls["count"] == 1:
            log_path.write_text(
                "ERROR: env file /workspace/jungle-worktrees/jungle-secrets/instica/.env.local not found\n",
                encoding="utf-8",
            )
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": [],
                },
            )
            raise typer.Exit(1)

        link_path = tmp_path / "worktrees" / "jungle-secrets"
        assert link_path.is_symlink()
        log_path.write_text("ok\n", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc124",
                "touched_files": [],
            },
        )

    model_called = {"value": False}

    def _model_fix(**kwargs: Any):
        model_called["value"] = True
        return True, "model", 0.1, "debugger"

    commands: list[str] = []

    def _run_command(command: str, cwd: Path | None = None):
        commands.append(command)

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "detect_missing_mandate_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _model_fix)
    monkeypatch.setattr(cli_module, "run_command", _run_command)

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=5), active_branch=None)

    assert result["status"] == "passed"
    assert preflight_calls["count"] == 2
    assert result["deterministic_repairs_used"] >= 1
    assert result["last_repair_kind"] == "missing-workspace-link"
    assert result["last_repair_result"] == "success"
    assert result["model_fixes_used"] == 0
    assert result["estimated_cost_used"] == pytest.approx(0.0)
    assert model_called["value"] is False
    assert not any("docker prune" in command for command in commands)


def test_loop_stops_when_same_missing_link_failure_repeats_after_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_workspace_link_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str) -> None:
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        log_path.write_text(
            "ERROR: env file /workspace/jungle-worktrees/jungle-secrets/instica/.env.local not found\n",
            encoding="utf-8",
        )
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:00Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 1,
                "passed": False,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc123",
                "touched_files": [],
            },
        )
        raise typer.Exit(1)

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "detect_missing_mandate_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(
        cli_module,
        "_run_model_fix_attempt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model fix must not run for missing-workspace-link")),
    )

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=5), active_branch=None)

    assert preflight_calls["count"] == 2
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "repeated_failure_after_repair"
    assert result["last_action"] == "repeated_failure_after_repair"
    assert result["last_repair_kind"] == "missing-workspace-link"
    assert result["deterministic_repairs_used"] == 1
    assert result["model_fixes_used"] == 0
    assert isinstance(result.get("last_log_path"), str)
    assert result["last_log_path"]


def test_loop_repairs_docker_runtime_network_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str) -> None:
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        if preflight_calls["count"] == 1:
            log_path.write_text(
                "Error response from daemon: container deadbeef is not connected to the network jungle-sample_default\n",
                encoding="utf-8",
            )
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": [],
                },
            )
            raise typer.Exit(1)

        log_path.write_text("ok\n", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc124",
                "touched_files": [],
            },
        )

    repair_calls = {"count": 0}

    def _run_repair(
        project_config,
        agent_state,
        *,
        kind,
        dry_run,
        allow_stash,
        active_branch_override,
        file_path=None,
        runtime_log_text=None,
    ):
        if kind == cli_module.RepairKind.docker_runtime_network:
            repair_calls["count"] += 1
            return cli_module.RepairResult(
                kind=kind,
                success=True,
                dry_run=dry_run,
                message="runtime repaired",
                log_path=run_dir / "repair_docker_runtime_network.log",
            )
        raise AssertionError(f"unexpected repair kind: {kind}")

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "run_repair", _run_repair)
    monkeypatch.setattr(cli_module, "detect_missing_mandate_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(
        cli_module,
        "_run_model_fix_attempt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model fix must not run for docker-runtime-network")),
    )

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=4), active_branch=None)

    assert result["status"] == "passed"
    assert preflight_calls["count"] == 2
    assert repair_calls["count"] == 1
    assert result["last_repair_kind"] == cli_module.RepairKind.docker_runtime_network.value
    assert result["model_fixes_used"] == 0


def test_loop_stops_when_docker_runtime_network_persists_after_one_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _project_file, _worktree, run_dir = _setup_agent(tmp_path)

    preflight_calls = {"count": 0}

    def _preflight(agent: str, project: str) -> None:
        preflight_calls["count"] += 1
        log_path = run_dir / "preflight.log"
        log_path.write_text(
            "Error response from daemon: error while removing network: network jungle-sample_default has active endpoints\n",
            encoding="utf-8",
        )
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:00Z",
                "command": "make mandate-preflight MANDATE_SLUG=loop-test",
                "exit_code": 1,
                "passed": False,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc123",
                "touched_files": [],
            },
        )
        raise typer.Exit(1)

    repair_calls = {"count": 0}

    def _run_repair(
        project_config,
        agent_state,
        *,
        kind,
        dry_run,
        allow_stash,
        active_branch_override,
        file_path=None,
        runtime_log_text=None,
    ):
        if kind == cli_module.RepairKind.docker_runtime_network:
            repair_calls["count"] += 1
            return cli_module.RepairResult(
                kind=kind,
                success=True,
                dry_run=dry_run,
                message="runtime repaired",
                log_path=run_dir / "repair_docker_runtime_network.log",
            )
        raise AssertionError(f"unexpected repair kind: {kind}")

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "run_repair", _run_repair)
    monkeypatch.setattr(cli_module, "detect_missing_mandate_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "get_touched_files", lambda worktree: [])
    monkeypatch.setattr(
        cli_module,
        "_run_model_fix_attempt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model fix must not run for docker-runtime-network")),
    )

    result = run_auto_repair_loop(project="jungle", agent="a1", options=_options(max_iterations=4), active_branch=None)

    assert preflight_calls["count"] == 2
    assert repair_calls["count"] == 1
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "repeated_failure_after_repair"
    assert result["model_fixes_used"] == 0
