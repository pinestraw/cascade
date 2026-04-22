from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command

from cascade.cli import app


def _cli_command_names() -> set[str]:
    click_app = get_command(app)
    return set(click_app.commands.keys())


def _workflow_cascade_commands(makefile_text: str) -> set[str]:
    # Parse only recipe lines to avoid examples/comments.
    recipe_lines = [line for line in makefile_text.splitlines() if line.startswith("\t")]

    # Restrict to workflow-related cascade invocations.
    workflow_recipe_lines = [
        line for line in recipe_lines
        if "cascade " in line and any(
            token in line
            for token in (
                "start", "check", "fix", "finish", "next",
                "preflight", "gate-summary", "continue", "mark",
                "gate-status", "status --project", "logs", "context-pack",
                "estimate-cost", "prepare-model-call",
            )
        )
    ]

    commands: set[str] = set()
    for line in workflow_recipe_lines:
        for match in re.findall(r"cascade\s+([a-z][a-z-]*)", line):
            commands.add(match)
    return commands


def test_makefile_workflow_targets_map_to_existing_cli_commands() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    make_commands = _workflow_cascade_commands(text)
    cli_commands = _cli_command_names()

    missing = sorted(make_commands - cli_commands)
    assert not missing, f"Makefile references missing CLI commands: {missing}"


def test_makefile_start_uses_start_command() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "cascade start" in text


def test_makefile_high_level_targets_use_existing_commands() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "cascade start" in text
    assert "cascade check" in text
    assert "cascade fix" in text
    assert "cascade finish" in text
    assert "cascade next" in text
