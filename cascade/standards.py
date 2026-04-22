from __future__ import annotations

from pathlib import Path

from cascade.config import ProjectConfig, instruction_file_paths
from cascade.shell import CommandError, run_command


def validate_worktree_location(project: ProjectConfig, worktree: Path) -> tuple[bool, str]:
    resolved_root = project.paths.worktree_root.resolve()
    resolved_worktree = worktree.resolve()
    try:
        resolved_worktree.relative_to(resolved_root)
    except ValueError:
        return (
            False,
            (
                f"Worktree {resolved_worktree} is outside configured worktree_root "
                f"{resolved_root}."
            ),
        )
    return True, "Worktree is inside configured worktree_root."


def validate_instruction_files(project: ProjectConfig) -> list[str]:
    warnings: list[str] = []
    for file_path in instruction_file_paths(project):
        if not file_path.exists():
            warnings.append(f"Instruction file not found: {file_path}")
    return warnings


def get_git_status(worktree: Path) -> str:
    try:
        return run_command("git status --short", cwd=worktree).stdout.strip()
    except CommandError:
        return "(unable to read git status)"


def get_git_diff_stat(worktree: Path) -> str:
    try:
        return run_command("git diff --stat", cwd=worktree).stdout.strip()
    except CommandError:
        return "(unable to read git diff --stat)"


def get_git_diff_names(worktree: Path) -> str:
    try:
        return run_command("git diff --name-only", cwd=worktree).stdout.strip()
    except CommandError:
        return "(unable to read git diff --name-only)"


def get_current_branch(worktree: Path) -> str:
    try:
        branch = run_command("git rev-parse --abbrev-ref HEAD", cwd=worktree).stdout.strip()
    except CommandError:
        return "(unable to read current branch)"
    return branch or "(detached HEAD)"


def expected_agent_branch(project: ProjectConfig, agent: str, slug: str) -> str:
    template = project.branches.agent_branch_template
    if template is None:
        return f"agent/{agent}/{slug}"
    return template.format(agent=agent, slug=slug)


def validate_agent_branch(project: ProjectConfig, agent_state: dict[str, object], worktree: Path) -> str | None:
    current = get_current_branch(worktree)
    if current.startswith("(unable") or current == "(detached HEAD)":
        return f"Unable to validate agent branch: current branch is '{current}'."
    expected = expected_agent_branch(project, str(agent_state["agent"]), str(agent_state["slug"]))
    if current != expected:
        return f"Branch mismatch: expected '{expected}', found '{current}'."
    return None
