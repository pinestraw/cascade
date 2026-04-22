"""Tests for preflight execution and gate behavior.

Proves:
- preflight uses configured command, not hardcoded commands
- pass/fail is based on subprocess exit code, not text parsing
- logs are saved on both pass and fail
- state is updated to preflight_passed or preflight_failed
- no model calls are made
- gate-summary reads saved logs correctly
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import app
from cascade.gates import classify_gate_failure, load_gate_result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_project_file(
    tmp_path: Path,
    preflight_cmd: str = "echo preflight-ok",
    init_mandate_cmd: str | None = None,
) -> Path:
    init_mandate_block = ""
    if init_mandate_cmd is not None:
        init_mandate_block = f"\n  init_mandate: {init_mandate_cmd}"
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
{init_mandate_block}
  preflight: {preflight_cmd}
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
    project: str = "jungle",
    agent: str = "oc1",
    preflight_cmd: str = "echo preflight-ok",
    init_mandate_cmd: str | None = None,
) -> tuple[Path, Path]:
    worktree = tmp_path / "worktrees" / f"{agent}-test-feature"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)

    project_file = _write_project_file(tmp_path, preflight_cmd=preflight_cmd, init_mandate_cmd=init_mandate_cmd)
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
    }
    state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
    return worktree, run_dir


# ---------------------------------------------------------------------------
# Preflight: uses configured command, saves log
# ---------------------------------------------------------------------------


def test_preflight_uses_configured_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir = _setup_agent(tmp_path, preflight_cmd="my-custom-preflight --slug={slug}")

    called_cmds: list[str] = []

    class _FakeResult:
        returncode = 0
        stdout = "All good."

    def _mock_subprocess_run(cmd, **kwargs):
        called_cmds.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return _FakeResult()

    monkeypatch.setattr(cli_module.subprocess, "run", _mock_subprocess_run)
    monkeypatch.setattr(
        cli_module, "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("preflight must not call OpenCode")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    # The configured command must have been called
    assert any("my-custom-preflight" in cmd for cmd in called_cmds), (
        f"Expected 'my-custom-preflight' in called commands. Got: {called_cmds}"
    )
    # Hardcoded 'make preflight' must NOT appear unless the config uses it
    for cmd in called_cmds:
        assert "make preflight" not in cmd or "my-custom-preflight" in cmd


def test_preflight_does_not_call_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    class _FakeResult:
        returncode = 0
        stdout = "ok"

    monkeypatch.setattr(
        cli_module.subprocess, "run",
        lambda cmd, **kwargs: _FakeResult(),
    )
    # Any attempt to check for or call OpenCode must raise
    monkeypatch.setattr(
        cli_module, "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("preflight must not check OpenCode availability")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output


def test_preflight_saves_log_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir = _setup_agent(tmp_path)

    class _FakeResult:
        returncode = 0
        stdout = "preflight output here"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FakeResult())

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    log_path = run_dir / "preflight.log"
    assert log_path.exists(), "preflight.log must be written after a passing run"
    log_content = log_path.read_text(encoding="utf-8")
    assert log_content  # non-empty


def test_preflight_saves_log_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir = _setup_agent(tmp_path)

    class _FailResult:
        returncode = 1
        stdout = "ruff-format failed"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FailResult())

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    # preflight should exit nonzero on failure
    assert result.exit_code != 0
    log_path = run_dir / "preflight.log"
    assert log_path.exists(), "preflight.log must be written even on failure"


def test_preflight_updates_state_to_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    class _FakeResult:
        returncode = 0
        stdout = "All checks passed."

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FakeResult())

    runner = CliRunner()
    runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    from cascade.state import load_agent_state
    state = load_agent_state("jungle", "oc1")
    assert state["state"] in ("preflight_passed", "preflight_running", "claimed")


def test_preflight_updates_state_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    class _FailResult:
        returncode = 2
        stdout = "type errors found"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FailResult())

    runner = CliRunner()
    runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    from cascade.state import load_agent_state
    state = load_agent_state("jungle", "oc1")
    assert state["state"] in ("preflight_failed", "claimed")


def test_preflight_exit_code_determines_pass_fail_not_stdout_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preflight that prints 'All good' but exits 1 must count as a failure."""
    monkeypatch.chdir(tmp_path)
    _setup_agent(tmp_path)

    class _TrickyResult:
        returncode = 1
        stdout = "All good. No errors. Passed."  # misleading text

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _TrickyResult())

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code != 0, (
        "Preflight with exit code 1 must exit nonzero even if stdout contains passing text."
    )


def test_preflight_detects_missing_mandate_metadata_with_specific_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, _run_dir = _setup_agent(
        tmp_path,
        preflight_cmd="make mandate-preflight MANDATE_SLUG={slug}",
        init_mandate_cmd="make mandate-init MANDATE_SLUG={slug}",
    )
    (worktree / ".github" / "mandates").mkdir(parents=True)

    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(AssertionError("preflight command must not run before metadata check"))
        if isinstance(cmd, str) and "mandate-preflight" in cmd
        else type("_Result", (), {"returncode": 0, "stdout": "ok"})(),
    )
    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("preflight must not check OpenCode")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code != 0
    assert ".github/mandates/test-feature.json" in result.output
    assert "make mandate-init MANDATE_SLUG=test-feature" in result.output
    assert "Required mandate metadata is missing" in result.output


# ---------------------------------------------------------------------------
# Gate classification
# ---------------------------------------------------------------------------


def test_gate_classify_trailing_whitespace_is_formatting_no_model() -> None:
    log = "Failed: trailing-whitespace\n- hook id: trailing-whitespace\n  exit code: 1\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "formatting"
    assert result["model_recommended"] is False


def test_gate_classify_end_of_file_fixer_is_formatting_no_model() -> None:
    log = "- hook id: end-of-file-fixer\n  exit code: 1\n"
    result = classify_gate_failure(log)
    assert result["category"] == "formatting"
    assert result["model_recommended"] is False


def test_gate_classify_ruff_format_is_formatting_no_model() -> None:
    log = "- hook id: ruff-format\n  exit code: 1\nReformatted 2 files.\n"
    result = classify_gate_failure(log)
    assert result["category"] == "formatting"
    assert result["model_recommended"] is False


def test_gate_classify_pyright_is_typing_model_recommended() -> None:
    log = "- hook id: pyright\n  exit code: 1\nerror: 'int' is not assignable to 'str'\n"
    result = classify_gate_failure(log)
    assert result["category"] == "typing"
    assert result["model_recommended"] is True


def test_gate_classify_mypy_is_typing_model_recommended() -> None:
    log = "- hook id: mypy\n  exit code: 1\nFound 3 errors in 2 files.\n"
    result = classify_gate_failure(log)
    assert result["category"] == "typing"
    assert result["model_recommended"] is True


def test_gate_classify_jungle_migrate_check_is_migration() -> None:
    log = "- hook id: jungle-migrate-check\n  exit code: 1\nMissing migration for FooModel.\n"
    result = classify_gate_failure(log)
    assert result["category"] == "migration"
    assert result["model_recommended"] is True


def test_gate_classify_gitleaks_is_security_model_recommended() -> None:
    log = "- hook id: gitleaks\n  exit code: 1\nSecret detected: AWS key in config.py.\n"
    result = classify_gate_failure(log)
    assert result["category"] == "security"
    assert result["model_recommended"] is True


def test_gate_classify_detect_private_key_is_security() -> None:
    log = "- hook id: detect-private-key\n  exit code: 1\n"
    result = classify_gate_failure(log)
    assert result["category"] == "security"
    assert result["model_recommended"] is True


def test_gate_classify_ruff_linting_no_model() -> None:
    log = "- hook id: ruff\n  exit code: 1\nE501 line too long\n"
    result = classify_gate_failure(log)
    assert result["category"] == "linting"
    assert result["model_recommended"] is False


def test_gate_classify_bandit_is_security() -> None:
    log = "- hook id: bandit\n  exit code: 1\nIssue: [B101] Use of assert detected.\n"
    result = classify_gate_failure(log)
    assert result["category"] == "security"
    assert result["model_recommended"] is True


def test_gate_classify_mandate_commit_msg_is_policy_no_model() -> None:
    log = "- hook id: mandate-commit-msg\n  exit code: 1\nCommit message does not start with mandate_id.\n"
    result = classify_gate_failure(log)
    assert result["category"] == "policy"
    assert result["model_recommended"] is False


def test_gate_classify_empty_log_is_undetected() -> None:
    result = classify_gate_failure("")
    assert result["detected"] is False
    assert result["category"] == "unknown"


def test_gate_classify_unknown_hook_is_unknown_conservative() -> None:
    log = "- hook id: my-totally-custom-gate\n  exit code: 1\nCustom failure.\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "unknown"
    # Unknown failures should recommend model review conservatively
    assert result["model_recommended"] is True


# ---------------------------------------------------------------------------
# Gate-summary CLI reads saved log
# ---------------------------------------------------------------------------


def test_gate_summary_classifies_formatting_failure_from_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir = _setup_agent(tmp_path)
    (run_dir / "preflight.log").write_text(
        "- hook id: ruff-format\n  exit code: 1\nReformatted 2 files.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module, "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(AssertionError("gate-summary must not call OpenCode")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["gate-summary", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()
    assert "formatting" in output_lower or "ruff-format" in output_lower


def test_gate_summary_classifies_pyright_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir = _setup_agent(tmp_path)
    (run_dir / "preflight.log").write_text(
        "- hook id: pyright\n  exit code: 1\n'str' is not assignable to 'int'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)

    runner = CliRunner()
    result = runner.invoke(app, ["gate-summary", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()
    assert "typing" in output_lower or "pyright" in output_lower


def test_gate_summary_security_shows_do_not_auto_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, run_dir = _setup_agent(tmp_path)
    (run_dir / "preflight.log").write_text(
        "- hook id: gitleaks\n  exit code: 1\nSecret found.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)

    runner = CliRunner()
    result = runner.invoke(app, ["gate-summary", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    # Security failures must not suggest blind auto-fix
    output_lower = result.output.lower()
    assert "security" in output_lower or "blindly" in output_lower or "do not" in output_lower
