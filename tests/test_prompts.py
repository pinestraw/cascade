from __future__ import annotations

from pathlib import Path

from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig
from cascade.prompts import build_launch_prompt


def test_launch_prompt_includes_required_context(tmp_path: Path) -> None:
    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "worktrees"),
        commands=CommandsConfig(create_worktree="echo create", preflight="echo preflight"),
    )
    agent_state = {
        "agent": "oc1",
        "issue": 45,
        "title": "Daily Digest Email",
        "worktree": "/tmp/jungle-worktrees/oc1-daily-digest-email",
    }

    prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body="Implement the feature safely.",
        instruction_files=[Path("/repo/COPILOT.md"), Path("/repo/.github/copilot-instructions.md")],
    )

    assert "Cascade Agent `oc1`" in prompt
    assert "project `jungle`" in prompt
    assert "GitHub issue #45: Daily Digest Email" in prompt
    assert "/tmp/jungle-worktrees/oc1-daily-digest-email" in prompt
    assert "Implement the feature safely." in prompt
    assert "/repo/COPILOT.md" in prompt
    assert "Do not run destructive cleanup or removal commands unless explicitly told." in prompt
    assert "Do not edit pre-commit, pre-push, mandate gate, or enforcement code unless explicitly authorized." in prompt
    assert "Do not stage, commit, or push unless explicitly authorized." in prompt
    assert "Do not treat model output as proof of validation; only configured command exit codes count." in prompt
    assert "Before closeout, run the configured preflight command." in prompt