from __future__ import annotations

from pathlib import Path

import pytest

from cascade.conversation import (
    build_continue_prompt,
    ensure_conversation_files,
    read_tail_chars,
)


def test_conversation_file_initialization(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "demo" / "runs" / "oc1"

    ensure_conversation_files(run_dir)

    expected_files = [
        "questions.md",
        "decisions.md",
        "running_summary.md",
        "transcript.md",
        "context.md",
        "diff.md",
        "opencode_session_id.txt",
        "continue_prompt.md",
        "preflight.log",
    ]
    for filename in expected_files:
        assert (run_dir / filename).exists()


def test_build_continue_prompt_contains_capsule_sections() -> None:
    prompt = build_continue_prompt(
        issue=45,
        title="Daily Digest Email",
        mandate="Do the thing",
        running_summary="Summary",
        decisions="Decision",
        questions="Question",
        preflight_log="PASS",
    )

    assert "GitHub issue #45: Daily Digest Email" in prompt
    assert "Mandate:" in prompt
    assert "Running summary:" in prompt
    assert "Decisions:" in prompt
    assert "Open questions:" in prompt
    assert "Latest preflight log excerpt:" in prompt


def test_bounded_transcript_reading(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.md"
    transcript.write_text("a" * 10 + "tail", encoding="utf-8")

    assert read_tail_chars(transcript, 4) == "tail"
    assert read_tail_chars(transcript, 100) == ("a" * 10 + "tail")


def test_clarify_appends_decisions_and_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project = "jungle"
    agent = "oc1"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    state_dir = tmp_path / "state" / project
    (state_dir / "agents").mkdir(parents=True)
    run_dir = state_dir / "runs" / agent
    run_dir.mkdir(parents=True)

    (state_dir / "agents" / f"{agent}.json").write_text(
        """
{
  "project": "jungle",
  "agent": "oc1",
  "issue": 45,
  "title": "Daily Digest",
  "slug": "daily-digest",
  "engine": "opencode",
  "model": "openrouter/z-ai/glm-4.7-flash",
  "state": "claimed",
  "worktree": "WT_PLACEHOLDER",
  "run_dir": "RUN_PLACEHOLDER"
}
""".replace("WT_PLACEHOLDER", str(worktree)).replace("RUN_PLACEHOLDER", str(run_dir)).strip()
        + "\n",
        encoding="utf-8",
    )

    from cascade import cli as cli_module
    from typer.testing import CliRunner

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: None)
    monkeypatch.setattr(
        cli_module,
        "run_prompt",
        lambda prompt, worktree, model, mode=None, use_continue=True: "Acknowledged.",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "clarify",
            agent,
            "--project",
            project,
            "--message",
            "Use Django Constance for the feature flag.",
        ],
    )

    assert result.exit_code == 0
    assert "Acknowledged." in result.output
    assert "Django Constance" in (run_dir / "decisions.md").read_text(encoding="utf-8")
    assert "Acknowledged." in (run_dir / "transcript.md").read_text(encoding="utf-8")
