from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import app
from cascade.gates import save_gate_result


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "worktrees" / "a1-test"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True)
    (run_dir / "mandate.md").write_text("# Mandate\n", encoding="utf-8")

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
  preflight: echo preflight
  done: echo mandate-done
  propagate: echo mandate-propagate
branches:
  agent_branch_template: agent/{{agent}}/{{slug}}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)

    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 1,
        "title": "Test",
        "slug": "test",
        "state": "closeout_ready",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }
    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    save_gate_result(
        run_dir,
        {
            "timestamp": "2026-04-23T00:00:00Z",
            "command": "echo preflight",
            "exit_code": 0,
            "passed": True,
            "log_path": str(run_dir / "preflight.log"),
            "git_head_sha": "deadbeef",
            "diff_fingerprint": "abc",
            "touched_files": [],
        },
    )
    return worktree, run_dir


def test_closeout_runs_done_and_marks_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)

    calls: list[str] = []

    def _run(cmd: str, cwd: Path | None = None):
        calls.append(cmd)

        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run)
    monkeypatch.setattr(cli_module, "check_gate_staleness", lambda gate, wt: (False, ""))
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: "agent/a1/test")
    monkeypatch.setattr(cli_module, "get_git_head_sha", lambda wt: "cafebabe")

    runner = CliRunner()
    result = runner.invoke(app, ["closeout", "a1", "--project", "jungle", "--yes"])

    assert result.exit_code == 0, result.output
    assert any("mandate-done" in cmd for cmd in calls)
    assert any("mandate-propagate" in cmd for cmd in calls)

    payload = json.loads((tmp_path / "state" / "jungle" / "agents" / "a1.json").read_text(encoding="utf-8"))
    assert payload["state"] == "closed"
    assert payload["squash_commit_sha"] == "cafebabe"


def test_closeout_failure_marks_closeout_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)

    def _run(cmd: str, cwd: Path | None = None):
        if "mandate-done" in cmd:
            raise cli_module.CommandError(cmd=cmd, cwd=cwd, exit_code=1, output="boom")

        class _R:
            stdout = ""

        return _R()

    monkeypatch.setattr(cli_module, "run_command", _run)
    monkeypatch.setattr(cli_module, "check_gate_staleness", lambda gate, wt: (False, ""))
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: "agent/a1/test")

    runner = CliRunner()
    result = runner.invoke(app, ["closeout", "a1", "--project", "jungle", "--yes"])

    assert result.exit_code != 0
    payload = json.loads((tmp_path / "state" / "jungle" / "agents" / "a1.json").read_text(encoding="utf-8"))
    assert payload["state"] == "closeout_failed"


def test_closeout_retries_once_for_docker_runtime_network_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)

    calls: list[str] = []
    mandate_done_attempts = {"count": 0}

    def _run(cmd: str, cwd: Path | None = None):
        calls.append(cmd)
        if "mandate-done" in cmd:
            mandate_done_attempts["count"] += 1
            if mandate_done_attempts["count"] == 1:
                raise cli_module.CommandError(
                    cmd=cmd,
                    cwd=cwd,
                    exit_code=1,
                    output=(
                        "Error response from daemon: container deadbeef is not connected to the network "
                        "jungle-sample_default"
                    ),
                )

        class _R:
            stdout = ""

        return _R()

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
        return cli_module.RepairResult(
            kind=kind,
            success=True,
            dry_run=dry_run,
            message="runtime repaired",
            log_path=Path(str(agent_state["run_dir"])) / "repair_docker_runtime_network.log",
        )

    monkeypatch.setattr(cli_module, "run_command", _run)
    monkeypatch.setattr(cli_module, "run_repair", _run_repair)
    monkeypatch.setattr(cli_module, "check_gate_staleness", lambda gate, wt: (False, ""))
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: "agent/a1/test")
    monkeypatch.setattr(cli_module, "get_git_head_sha", lambda wt: "cafebabe")

    runner = CliRunner()
    result = runner.invoke(app, ["closeout", "a1", "--project", "jungle", "--yes"])

    assert result.exit_code == 0, result.output
    assert repair_calls["count"] == 1
    assert mandate_done_attempts["count"] == 2
    payload = json.loads((tmp_path / "state" / "jungle" / "agents" / "a1.json").read_text(encoding="utf-8"))
    assert payload["state"] == "closed"


def test_closeout_marks_failed_when_docker_runtime_network_persists_after_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)

    attempts = {"count": 0}

    def _run(cmd: str, cwd: Path | None = None):
        if "mandate-done" in cmd:
            attempts["count"] += 1
            raise cli_module.CommandError(
                cmd=cmd,
                cwd=cwd,
                exit_code=1,
                output=(
                    "Error response from daemon: error while removing network: network "
                    "jungle-sample_default has active endpoints"
                ),
            )

        class _R:
            stdout = ""

        return _R()

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
            log_path=Path(str(agent_state["run_dir"])) / "repair_docker_runtime_network.log",
        )

    monkeypatch.setattr(cli_module, "run_command", _run)
    monkeypatch.setattr(cli_module, "run_repair", _run_repair)
    monkeypatch.setattr(cli_module, "check_gate_staleness", lambda gate, wt: (False, ""))
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: "agent/a1/test")

    runner = CliRunner()
    result = runner.invoke(app, ["closeout", "a1", "--project", "jungle", "--yes"])

    assert result.exit_code != 0
    assert attempts["count"] == 2
    assert "persisted after deterministic closeout retry" in result.output
    payload = json.loads((tmp_path / "state" / "jungle" / "agents" / "a1.json").read_text(encoding="utf-8"))
    assert payload["state"] == "closeout_failed"
