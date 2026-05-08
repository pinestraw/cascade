from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cascade.cli import app
from cascade import opencode as opencode_module
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


def test_supports_non_interactive_run_returns_true_when_help_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(opencode_module.shutil, "which", lambda name: "/usr/bin/opencode")

    def _run(cmd, **kwargs):
        if cmd == ["opencode", "--help"]:
            return type("_R", (), {"returncode": 0, "stdout": "Commands: run chat", "stderr": ""})()
        if cmd == ["opencode", "run", "--help"]:
            return type("_R", (), {"returncode": 0, "stdout": "Usage: opencode run", "stderr": ""})()
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(opencode_module.subprocess, "run", _run)

    supported, reason = opencode_module.supports_non_interactive_run()

    assert supported is True
    assert reason is None


def test_supports_non_interactive_run_returns_false_when_run_subcommand_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(opencode_module.shutil, "which", lambda name: "/usr/bin/opencode")

    def _run(cmd, **kwargs):
        if cmd == ["opencode", "--help"]:
            return type("_R", (), {"returncode": 0, "stdout": "Commands: chat", "stderr": ""})()
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(opencode_module.subprocess, "run", _run)

    supported, reason = opencode_module.supports_non_interactive_run()

    assert supported is False
    assert reason is not None
    assert "run" in reason
