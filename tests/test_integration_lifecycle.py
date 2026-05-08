from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade.cli import app
from cascade.gates import save_gate_result


pytestmark = pytest.mark.integration


def _bootstrap(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "worktrees").mkdir(parents=True, exist_ok=True)
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
branches:
  agent_branch_template: agent/{{agent}}/{{slug}}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    worktree = tmp_path / "worktrees" / "a1-feature"
    worktree.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mandate.md").write_text("# Mandate\n", encoding="utf-8")

    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 7,
        "title": "Feature",
        "slug": "feature",
        "state": "closeout_ready",
        "worktree": str(worktree),
        "project_file": str(project_file),
        "run_dir": str(run_dir),
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


def test_closeout_transitions_to_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _bootstrap(tmp_path)
    monkeypatch.setattr(cli_module, "run_command", lambda cmd, cwd=None: type("R", (), {"stdout": ""})())
    monkeypatch.setattr(cli_module, "check_gate_staleness", lambda gate, wt: (False, ""))
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: "agent/a1/feature")
    monkeypatch.setattr(cli_module, "get_git_head_sha", lambda wt: "cafebabe")

    result = CliRunner().invoke(app, ["closeout", "a1", "--project", "jungle", "--yes"])
    assert result.exit_code == 0, result.output

    payload = json.loads((tmp_path / "state" / "jungle" / "agents" / "a1.json").read_text(encoding="utf-8"))
    assert payload["state"] == "closed"
    assert payload["squash_commit_sha"] == "cafebabe"
