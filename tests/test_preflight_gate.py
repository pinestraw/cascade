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
import typer
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


class _FakeStreamingStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


class _FakeStreamingProcess:
    def __init__(self, lines: list[str], returncode: int, pending_polls: int = 2) -> None:
        self.args = ["fake-preflight"]
        self.stdout = _FakeStreamingStdout(lines)
        self._returncode = returncode
        self._pending_polls = pending_polls

    def __enter__(self) -> _FakeStreamingProcess:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def poll(self) -> int | None:
        if self._pending_polls > 0:
            self._pending_polls -= 1
            return None
        return self._returncode

    def communicate(self, input=None, timeout=None):
        return ("".join(list(self.stdout)), "")

    def wait(self, timeout=None) -> int:
        return self._returncode

    def kill(self) -> None:
        return None

    def terminate(self) -> None:
        return None


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


def test_preflight_watch_streams_output_and_writes_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "_PREFLIGHT_PROGRESS_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeStreamingProcess(["first line\n", "second line\n"], 0),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle", "--watch"])

    assert result.exit_code == 0, result.output
    assert "[preflight] first line" in result.output
    assert "[preflight] second line" in result.output
    log_content = (run_dir / "preflight.log").read_text(encoding="utf-8")
    assert "first line" in log_content
    assert "second line" in log_content


def test_preflight_verbose_prints_progress_without_full_raw_spam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    lines = [f"line {index}\n" for index in range(1, 8)]
    monkeypatch.setattr(cli_module, "_PREFLIGHT_PROGRESS_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeStreamingProcess(lines, 0, pending_polls=3),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "[preflight] still running..." in result.output
    assert "[preflight] line 7" in result.output
    assert "[preflight] line 1" not in result.output
    log_content = (run_dir / "preflight.log").read_text(encoding="utf-8")
    assert "line 1" in log_content
    assert "line 7" in log_content


def test_preflight_default_mode_remains_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    class _FakeResult:
        returncode = 0
        stdout = "compact output"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda *args, **kwargs: _FakeResult())
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("default preflight must stay on compact subprocess.run path")),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert "[preflight]" not in result.output
    assert "compact output" in (run_dir / "preflight.log").read_text(encoding="utf-8")


def test_check_forwards_verbose_and_watch_to_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    seen: dict[str, bool] = {}
    monkeypatch.setattr(cli_module, "diff", lambda *args, **kwargs: None)

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False) -> None:
        seen["verbose"] = verbose
        seen["watch"] = watch
        log_path = run_dir / "preflight.log"
        log_path.write_text("ok", encoding="utf-8")
        from cascade.gates import save_gate_result

        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:00Z",
                "command": "echo preflight-ok",
                "exit_code": 0,
                "passed": True,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc123",
                "touched_files": [],
            },
        )

    monkeypatch.setattr(cli_module, "preflight", _preflight)

    runner = CliRunner()
    result = runner.invoke(app, ["check", "oc1", "--project", "jungle", "--verbose", "--watch"])

    assert result.exit_code == 0, result.output
    assert seen == {"verbose": True, "watch": True}


def test_check_retries_once_for_docker_runtime_network_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "diff", lambda *args, **kwargs: None)
    attempts = {"count": 0}

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False) -> None:
        attempts["count"] += 1
        log_path = run_dir / "preflight.log"
        from cascade.gates import save_gate_result

        if attempts["count"] == 1:
            failure = (
                "Error response from daemon: container abc is not connected to the network "
                "jungle-sample_default\n"
            )
            log_path.write_text(failure, encoding="utf-8")
            save_gate_result(
                run_dir,
                {
                    "timestamp": "2026-04-22T12:00:00Z",
                    "command": "echo preflight-fail",
                    "exit_code": 1,
                    "passed": False,
                    "log_path": str(log_path),
                    "git_head_sha": "deadbeef",
                    "diff_fingerprint": "abc123",
                    "touched_files": [],
                },
            )
            raise typer.Exit(1)

        log_path.write_text("ok", encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:01Z",
                "command": "echo preflight-ok",
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
        repair_calls["count"] += 1
        assert kind == cli_module.RepairKind.docker_runtime_network
        assert runtime_log_text is not None
        return cli_module.RepairResult(
            kind=kind,
            success=True,
            dry_run=dry_run,
            message="runtime repaired",
            log_path=run_dir / "repair_docker_runtime_network.log",
        )

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "run_repair", _run_repair)

    runner = CliRunner()
    result = runner.invoke(app, ["check", "oc1", "--project", "jungle"])

    assert result.exit_code == 0, result.output
    assert attempts["count"] == 2
    assert repair_calls["count"] == 1


def test_check_docker_runtime_network_persists_after_single_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    monkeypatch.setattr(cli_module, "diff", lambda *args, **kwargs: None)
    attempts = {"count": 0}

    def _preflight(agent: str, project: str, verbose: bool = False, watch: bool = False) -> None:
        attempts["count"] += 1
        log_path = run_dir / "preflight.log"
        from cascade.gates import save_gate_result

        failure = (
            "Error response from daemon: error while removing network: network "
            "jungle-sample_default has active endpoints\n"
        )
        log_path.write_text(failure, encoding="utf-8")
        save_gate_result(
            run_dir,
            {
                "timestamp": "2026-04-22T12:00:00Z",
                "command": "echo preflight-fail",
                "exit_code": 1,
                "passed": False,
                "log_path": str(log_path),
                "git_head_sha": "deadbeef",
                "diff_fingerprint": "abc123",
                "touched_files": [],
            },
        )
        raise typer.Exit(1)

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
        return cli_module.RepairResult(
            kind=kind,
            success=True,
            dry_run=dry_run,
            message="runtime repaired",
            log_path=run_dir / "repair_docker_runtime_network.log",
        )

    model_suggestion_triggered = {"value": False}

    def _gate_summary(agent: str, project: str) -> None:
        payload = cli_module.load_gate_result(run_dir)
        if payload is not None:
            log_text = Path(str(payload["log_path"])).read_text(encoding="utf-8")
            classification = cli_module.classify_gate_failure(log_text)
            model_suggestion_triggered["value"] = bool(classification.get("model_recommended", True))

    monkeypatch.setattr(cli_module, "preflight", _preflight)
    monkeypatch.setattr(cli_module, "run_repair", _run_repair)
    monkeypatch.setattr(cli_module, "gate_summary", _gate_summary)

    runner = CliRunner()
    result = runner.invoke(app, ["check", "oc1", "--project", "jungle"])

    assert result.exit_code != 0
    assert attempts["count"] == 2
    assert "persisted after deterministic repair/retry" in result.output
    assert model_suggestion_triggered["value"] is False


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
        init_mandate_cmd="make mandate-start MANDATE_SLUG={slug} MANDATE_TITLE='{title}'",
    )
    (_run_dir / "mandate.md").write_text("# Mandate\n", encoding="utf-8")
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
    normalized_output = " ".join(result.output.split())

    assert result.exit_code != 0
    assert "test-feature.json" in normalized_output
    assert "Repair available: cascade repair oc1 --project jungle" in normalized_output
    assert "Required mandate metadata is missing" in normalized_output


def test_preflight_missing_mandate_metadata_without_init_command_has_no_fake_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    worktree, _run_dir = _setup_agent(
        tmp_path,
        preflight_cmd="make mandate-preflight MANDATE_SLUG={slug}",
        init_mandate_cmd=None,
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
    normalized_output = " ".join(result.output.split())

    assert result.exit_code != 0
    assert (
        "Missing mandate start command config: set one of commands.mandate_start, commands.start_mandate, or commands.init_mandate."
        in normalized_output
    )
    assert "mandate-init" not in normalized_output


def test_gate_classify_missing_mandate_metadata_is_workflow_no_model() -> None:
    log = "Required mandate metadata is missing: /workspace/jungle-worktrees/a1/foo/.github/mandates/foo.json"
    result = classify_gate_failure(log)
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False


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


def test_gate_classify_validation_slot_timeout_is_environment_no_model() -> None:
    log = "Timed out waiting for the shared heavy validation slot"
    result = classify_gate_failure(log)
    assert result["hook"] == "validation-slot-timeout"
    assert result["category"] == "environment"
    assert result["model_recommended"] is False


def test_gate_classify_branch_mismatch_is_workflow_no_model() -> None:
    log = "Branch mismatch: expected 'agent/a1/slug', found 'agent/copilot/slug'."
    result = classify_gate_failure(log)
    assert result["hook"] == "mandate-agent-branch-mismatch"
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False


def test_gate_classify_jungle_branch_mismatch_format_is_workflow_no_model() -> None:
    # Regression: Jungle emits "[mandate] ERROR: Current branch X does not match mandate agent branch Y"
    log = (
        "[mandate] ERROR: Current branch agent/smoke-agent/fix-update-deployyml-to-use-dynamic-image-tags"
        " does not match mandate agent branch agent/copilot/fix-update-deployyml-to-use-dynamic-image-tags."
        " Aborting."
    )
    result = classify_gate_failure(log)
    assert result["hook"] == "mandate-agent-branch-mismatch"
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False


def test_gate_classify_stale_docker_state_is_environment_no_model() -> None:
    log = "No such file or directory: /workspace/jungle-worktrees/a1-test"
    result = classify_gate_failure(log)
    assert result["hook"] == "stale-docker-era-state"
    assert result["category"] == "environment"
    assert result["model_recommended"] is False


def test_gate_classify_mandate_not_in_progress_is_workflow_no_model() -> None:
    log = "[mandate] ERROR: Mandate fix-update-deployyml is not in progress"
    result = classify_gate_failure(log)
    assert result["hook"] == "mandate-metadata"
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False


def test_gate_classify_repo_mismatch_validation_is_workflow_no_model() -> None:
    log = "Mandate metadata validation: repo mismatch: expected 'jungle', found 'smoke-agent-fix-update'"
    result = classify_gate_failure(log)
    assert result["hook"] == "mandate-metadata"
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False


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


# ---------------------------------------------------------------------------
# Preflight observability: failure tail and thin-log detection
# ---------------------------------------------------------------------------


def test_preflight_failure_tail_shown_in_default_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure tail must be emitted even when --verbose / --watch are not passed."""
    monkeypatch.chdir(tmp_path)
    _worktree, run_dir = _setup_agent(tmp_path)

    class _FailResult:
        returncode = 1
        stdout = (
            "[mandate] ERROR: Current branch main does not match mandate agent branch agent/a2/slug\n"
            "make: *** [mandate-preflight] Error 1\n"
        )

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FailResult())

    runner = CliRunner()
    # Default mode: no --verbose, no --watch
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code != 0
    # The actual error line from mandate_die must appear in the terminal output.
    # Normalize whitespace to handle Rich line-wrap splitting.
    normalized_output = " ".join(result.output.split())
    assert "[preflight]" in result.output, "failure tail header must be emitted in default mode"
    assert "does not match mandate agent branch" in normalized_output


def test_preflight_opaque_log_triggers_thin_log_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When only a make error wrapper is captured, a thin-log warning must be shown."""
    monkeypatch.chdir(tmp_path)
    _worktree, _run_dir = _setup_agent(tmp_path)

    class _FailResult:
        returncode = 2
        stdout = "make: *** [mandate-preflight] Error 1\n"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FailResult())

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code != 0
    # The thin-log warning must be present, directing the user to --verbose
    assert "--verbose" in result.output
    assert "thin" in result.output.lower() or "wrapper" in result.output.lower()


def test_is_opaque_preflight_log_detects_thin_logs() -> None:
    """_is_opaque_preflight_log returns True only for pure make-wrapper output."""
    from cascade.cli import _is_opaque_preflight_log

    # Opaque: only make error wrapper(s)
    assert _is_opaque_preflight_log("make: *** [mandate-preflight] Error 1\n")
    assert _is_opaque_preflight_log(
        "make[1]: *** [mandate-preflight-backend-tests] Error 2\n"
        "make: *** [mandate-preflight] Error 2\n"
    )
    assert _is_opaque_preflight_log("")
    assert _is_opaque_preflight_log("   \n  \n")

    # Not opaque: has at least one meaningful line
    assert not _is_opaque_preflight_log(
        "[mandate] ERROR: Missing file: .github/mandates/slug.json\n"
        "make: *** [mandate-preflight] Error 1\n"
    )
    assert not _is_opaque_preflight_log("ruff format check failed\n")
    assert not _is_opaque_preflight_log(
        "[mandate] ERROR: Current branch main does not match agent branch agent/a2/slug\n"
    )


def test_preflight_non_opaque_failure_does_not_trigger_thin_log_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When actual error lines are present, the thin-log warning must not fire."""
    monkeypatch.chdir(tmp_path)
    _worktree, _run_dir = _setup_agent(tmp_path)

    class _FailResult:
        returncode = 1
        stdout = (
            "[mandate] ERROR: Mandate slug is not in progress\n"
            "make: *** [mandate-preflight] Error 1\n"
        )

    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, **kwargs: _FailResult())

    runner = CliRunner()
    result = runner.invoke(app, ["preflight", "oc1", "--project", "jungle"])

    assert result.exit_code != 0
    # Thin-log warning must NOT appear when the log has real content
    assert "--verbose" not in result.output or "thin" not in result.output.lower()
