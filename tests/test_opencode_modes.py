from __future__ import annotations

from typer.testing import CliRunner

from cascade.cli import app
from cascade.opencode import OpenCodeMode, mode_to_agent


def test_mode_to_agent_mapping() -> None:
    assert mode_to_agent(OpenCodeMode.plan) == "plan"
    assert mode_to_agent(OpenCodeMode.build) == "build"
    assert mode_to_agent(None) is None


def test_invalid_mode_rejected_for_chat_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["chat", "oc1", "--project", "jungle", "--mode", "invalid"])

    assert result.exit_code != 0
    assert "invalid" in result.output
