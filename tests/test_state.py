from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cascade.cli import app
from cascade.state import list_agent_states, load_agent_state, save_agent_state, update_agent_state


def test_save_load_list_and_update_agent_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    state = {
        "project": "demo",
        "agent": "oc1",
        "issue": 1,
        "title": "Example",
        "slug": "example",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": "/tmp/worktree",
        "run_dir": "/tmp/run",
    }

    save_agent_state("demo", "oc1", state)

    loaded = load_agent_state("demo", "oc1")
    assert loaded == state
    assert list_agent_states("demo") == [state]

    updated = update_agent_state("demo", "oc1", "running")
    assert updated["state"] == "running"
    assert load_agent_state("demo", "oc1")["state"] == "running"


def test_invalid_mark_state_is_rejected() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["mark", "oc1", "--project", "demo", "--state", "not-a-state"])

    assert result.exit_code != 0
    assert "not-a-state" in result.output