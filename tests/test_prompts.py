from __future__ import annotations

from pathlib import Path

from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig
from cascade.prompts import build_launch_prompt, build_task_prompt, get_task_output_rules


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


# ---------------------------------------------------------------------------
# Standards-preservation: all safety rules must be present
# ---------------------------------------------------------------------------


def _make_minimal_project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "worktrees"),
        commands=CommandsConfig(create_worktree="echo create", preflight="make mandate-preflight"),
    )


def _make_prompt(tmp_path: Path, worktree: str = "/tmp/wt") -> str:
    project = _make_minimal_project(tmp_path)
    return build_launch_prompt(
        project=project,
        agent_state={"agent": "oc1", "issue": 10, "title": "T", "worktree": worktree},
        mandate_body="Do the thing.",
        instruction_files=[Path("/repo/COPILOT.md")],
    )


def test_launch_prompt_says_work_only_inside_worktree(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path, worktree="/tmp/agent-worktree")
    assert "Work only inside the assigned worktree" in prompt


def test_launch_prompt_includes_assigned_worktree_path(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path, worktree="/tmp/agent-worktree")
    assert "/tmp/agent-worktree" in prompt


def test_launch_prompt_says_do_not_modify_unrelated_worktrees(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "unrelated worktrees" in prompt


def test_launch_prompt_says_prefer_make_targets(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    # The prompt should guide the agent toward Make targets, not ad-hoc shell
    prompt_lower = prompt.lower()
    assert "make" in prompt_lower


def test_launch_prompt_says_no_weaken_gates(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "Do not weaken" in prompt or "weaken" in prompt.lower()


def test_launch_prompt_says_no_stage_commit_push(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "stage" in prompt.lower() and "commit" in prompt.lower() and "push" in prompt.lower()


def test_launch_prompt_says_only_exit_codes_validate(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "configured command exit codes" in prompt or "exit codes" in prompt


def test_launch_prompt_includes_no_destructive_cleanup(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "destructive" in prompt.lower()


def test_launch_prompt_instruction_files_all_listed(tmp_path: Path) -> None:
    project = _make_minimal_project(tmp_path)
    agent_state = {"agent": "oc1", "issue": 1, "title": "T", "worktree": "/tmp/wt"}
    prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body="Do it.",
        instruction_files=[
            Path("/repo/COPILOT.md"),
            Path("/repo/.github/copilot-instructions.md"),
            Path("/repo/.github/AGENT_MANDATE_PROTOCOL.md"),
        ],
    )
    assert "/repo/COPILOT.md" in prompt
    assert "copilot-instructions.md" in prompt
    assert "AGENT_MANDATE_PROTOCOL.md" in prompt


def test_launch_prompt_says_no_precommit_editing(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "pre-commit" in prompt or "enforcement code" in prompt


def test_launch_prompt_says_run_preflight_before_closeout(tmp_path: Path) -> None:
    prompt = _make_prompt(tmp_path)
    assert "preflight" in prompt.lower()


def test_launch_prompt_no_instruction_files_shows_placeholder(tmp_path: Path) -> None:
    project = _make_minimal_project(tmp_path)
    agent_state = {"agent": "oc1", "issue": 1, "title": "T", "worktree": "/tmp/wt"}
    prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body="Do it.",
        instruction_files=[],
    )
    assert "No configured instruction files" in prompt


# ---------------------------------------------------------------------------
# Task-specific output discipline prompts
# ---------------------------------------------------------------------------


def test_diagnose_prompt_includes_output_discipline() -> None:
    rules = get_task_output_rules("diagnose")
    assert "Output discipline" in rules
    assert "root cause" in rules.lower()


def test_fix_prompt_limits_scope() -> None:
    rules = get_task_output_rules("fix")
    assert "only" in rules.lower() or "specific" in rules.lower()


def test_review_prompt_includes_mandate_compliance() -> None:
    rules = get_task_output_rules("review")
    assert "mandate" in rules.lower()
    assert "gate" in rules.lower()


def test_implement_prompt_says_no_unrelated_refactors() -> None:
    rules = get_task_output_rules("implement")
    assert "unrelated" in rules.lower()


def test_plan_prompt_says_ask_questions_first() -> None:
    rules = get_task_output_rules("plan")
    assert "question" in rules.lower()


def test_unknown_task_output_rules_returns_empty_string() -> None:
    rules = get_task_output_rules("nonexistent_task_xyz")
    assert rules == ""


def test_build_task_prompt_contains_context_and_rules() -> None:
    context = "# Context\n\nSome project context here."
    prompt = build_task_prompt(context, "diagnose")
    assert context in prompt
    assert "Output discipline" in prompt
    # Must not claim validation passed
    assert "validation" in prompt.lower() or "exit code" in prompt.lower()


def test_build_task_prompt_fix_does_not_include_irrelevant_rules() -> None:
    prompt = build_task_prompt("# Context", "fix")
    # Fix should not include 'plan'-type output about questions
    assert "numbered steps" not in prompt.lower()


def test_build_task_prompt_unknown_still_safe(tmp_path: Path) -> None:
    prompt = build_task_prompt("# Context", "unknown_xyz")
    assert "# Context" in prompt
    # Should still include the no-validation-claim reminder
    assert len(prompt) > 20

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