"""Deterministic context pack builder for model-backed Cascade workflows.

A context pack is a bounded, task-specific collection of relevant text
assembled without calling a model.  It is saved to disk so model-backed
commands receive compact, auditable context instead of raw dumps.

Safety rules enforced here:
- Secrets paths (config.paths.secrets_root) are never included.
- .env files and their contents are never included.
- Full git diff is only included when the budget explicitly requests it.
- Full transcript is excluded by default.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from cascade.config import ProjectConfig, instruction_file_paths
from cascade.costs import estimate_tokens
from cascade.gates import load_gate_result, check_gate_staleness, gate_status_line
from cascade.standards import (
    get_current_branch,
    get_git_diff_names,
    get_git_diff_stat,
    get_git_status,
)


# Tasks for which context packs can be built.
ALLOWED_TASKS = frozenset({"plan", "implement", "diagnose", "fix", "review", "summarize"})

# Safety: never include content from these directory or file names.
_BLOCKED_PATH_FRAGMENTS = frozenset({
    ".env",
    "secrets",
    "jungle-secrets",
    ".secret",
    "credentials",
    "private_key",
})


def _is_blocked_path(path: Path) -> bool:
    """Return True if the path contains a fragment that should never be included."""
    parts = {p.lower() for p in path.parts}
    name = path.name.lower()
    if name.startswith(".env"):
        return True
    return bool(parts & _BLOCKED_PATH_FRAGMENTS)


def _safe_read(path: Path, max_chars: int = 0) -> str:
    """Read a text file safely, returning empty string on error.

    Refuses to read blocked paths (secrets, .env, etc.).
    """
    if _is_blocked_path(path):
        return "(blocked: secrets path)"
    try:
        text = path.read_text(encoding="utf-8")
        if max_chars > 0:
            return text[-max_chars:]
        return text
    except (FileNotFoundError, OSError):
        return ""


def _tail_lines(path: Path, n_lines: int) -> str:
    """Return the last n_lines of a file."""
    if _is_blocked_path(path):
        return "(blocked: secrets path)"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n_lines:]) if lines else ""
    except (FileNotFoundError, OSError):
        return ""


@dataclass
class ContextPack:
    task_type: str
    generated_at: str
    project_name: str
    agent: str
    issue: int | str
    title: str
    slug: str
    worktree: str
    current_branch: str
    current_state: str
    estimated_input_tokens: int
    max_input_tokens: int
    truncated: bool
    included_sections: list[str]
    warnings: list[str]
    body: str  # The full assembled markdown text


def _operating_rules_block() -> str:
    return """## Operating safety rules (always included)

- Work only in the assigned worktree.
- Do not weaken gates, tests, coverage, typing, pre-commit, pre-push, or enforcement code.
- Do not stage, commit, or push unless explicitly authorized.
- Do not claim validation passed — only configured command exit codes determine pass/fail.
- If fixing, address only the specified failure; avoid unrelated refactors.
- Secrets are not included in this context pack.
"""


def build_context_pack(
    project: ProjectConfig,
    agent_state: dict[str, object],
    task_type: str,
    run_dir: Path,
    *,
    include_diff: bool = False,
) -> ContextPack:
    """Build a bounded, deterministic context pack for a given task.

    Parameters
    ----------
    project:    Loaded project config.
    agent_state: Agent state dict loaded from JSON.
    task_type:  One of ALLOWED_TASKS.
    run_dir:    Path to state/<project>/runs/<agent>/.
    include_diff: Override to force including full git diff names.
    """
    from cascade.conversation import timestamp_utc  # avoid circular

    if task_type not in ALLOWED_TASKS:
        raise ValueError(f"Unknown task type '{task_type}'. Allowed: {sorted(ALLOWED_TASKS)}")

    budget = project.context_budgets.for_task(task_type)
    generated_at = timestamp_utc()

    worktree_str = str(agent_state.get("worktree", ""))
    worktree = Path(worktree_str) if worktree_str else None

    # Git state (deterministic)
    current_branch = get_current_branch(worktree) if worktree else "(unknown)"
    git_status = get_git_status(worktree) if worktree else "(worktree not found)"
    git_diff_stat = ""
    if budget.include_diff_stat and worktree:
        git_diff_stat = get_git_diff_stat(worktree)

    # Gate result
    gate_result_path_str = str(agent_state.get("gate_result_path", ""))
    gate_result: dict[str, object] | None = None
    if gate_result_path_str:
        gate_result = load_gate_result(Path(gate_result_path_str).parent)
    gate_line = gate_status_line(gate_result, worktree)
    gate_failure_summary = ""
    if gate_result:
        fs = gate_result.get("failure_summary")
        gate_failure_summary = str(fs) if fs else ""

    # Capsule files
    mandate = _safe_read(run_dir / "mandate.md")
    decisions = _safe_read(run_dir / "decisions.md")
    questions = _safe_read(run_dir / "questions.md")
    running_summary = _safe_read(run_dir / "running_summary.md")
    preflight_log_tail = _tail_lines(run_dir / "preflight.log", budget.include_logs_tail_lines)
    diff_md = _safe_read(run_dir / "diff.md")

    # Instruction file names (not contents — contents stay in target repo)
    instruction_names: list[str] = []
    if budget.include_instruction_files:
        instruction_names = [str(p) for p in instruction_file_paths(project)]

    # Warnings
    warnings: list[str] = []
    secrets_root = project.paths.secrets_root
    if secrets_root is not None and worktree and str(secrets_root) in str(worktree):
        warnings.append("Worktree appears to be inside secrets_root — this is unusual.")

    # Build sections, track what is included
    included_sections: list[str] = []
    sections: list[str] = []

    # Always-included header
    sections.append(
        f"# Cascade Context Pack\n\n"
        f"- Task: {task_type}\n"
        f"- Generated: {generated_at}\n"
        f"- Project: {project.name}\n"
        f"- Agent: {agent_state.get('agent', '')}\n"
        f"- Issue: #{agent_state.get('issue', '')}\n"
        f"- Title: {agent_state.get('title', '')}\n"
        f"- Slug: {agent_state.get('slug', '')}\n"
        f"- State: {agent_state.get('state', '')}\n"
        f"- Worktree: {worktree_str}\n"
        f"- Branch: {current_branch}\n"
    )
    included_sections.append("header")

    sections.append(_operating_rules_block())
    included_sections.append("operating_rules")

    # Gate result
    gate_section = f"## Gate Result\n\n- Status: {gate_line}\n"
    if gate_result:
        gate_section += f"- Exit code: {gate_result.get('exit_code', '?')}\n"
        gate_section += f"- Log: {gate_result.get('log_path', '(unknown)')}\n"
    if gate_failure_summary:
        gate_section += f"\n### Failure summary\n\n{gate_failure_summary}\n"
    sections.append(gate_section)
    included_sections.append("gate_result")

    if instruction_names:
        sections.append(
            "## Configured instruction files (read these before editing)\n\n"
            + "\n".join(f"- {n}" for n in instruction_names)
            + "\n"
        )
        included_sections.append("instruction_files")

    if mandate:
        sections.append(f"## Mandate\n\n{mandate}\n")
        included_sections.append("mandate")

    if decisions:
        sections.append(f"## Decisions\n\n{decisions}\n")
        included_sections.append("decisions")

    if questions:
        sections.append(f"## Questions\n\n{questions}\n")
        included_sections.append("questions")

    if running_summary:
        sections.append(f"## Running Summary\n\n{running_summary}\n")
        included_sections.append("running_summary")

    if git_status:
        sections.append(f"## Git Status\n\n{git_status}\n")
        included_sections.append("git_status")

    if git_diff_stat:
        sections.append(f"## Git Diff Stat\n\n{git_diff_stat}\n")
        included_sections.append("git_diff_stat")

    if diff_md:
        sections.append(f"## Saved Diff Summary\n\n{diff_md}\n")
        included_sections.append("diff_md")

    if preflight_log_tail:
        sections.append(f"## Preflight Log Tail\n\n{preflight_log_tail}\n")
        included_sections.append("preflight_log_tail")

    if (include_diff or budget.include_full_diff) and worktree:
        diff_names = get_git_diff_names(worktree)
        if diff_names:
            sections.append(f"## Changed Files\n\n{diff_names}\n")
            included_sections.append("changed_files")

    body = "\n".join(sections)

    # Truncation check and enforcement
    estimated = estimate_tokens(body)
    truncated = False

    if estimated > budget.max_input_tokens:
        truncated = True
        warnings.append(
            f"Context pack estimated at ~{estimated:,} tokens, "
            f"exceeding budget of {budget.max_input_tokens:,}. "
            "Lower-priority sections were truncated."
        )
        # Truncate lower-priority sections in order of least importance
        # until we fit within budget. We drop from the body by rebuilding
        # without the lower-priority sections.
        truncation_order = [
            "preflight_log_tail",
            "running_summary",
            "decisions",
            "questions",
            "diff_md",
            "git_diff_stat",
        ]
        remaining_sections = list(included_sections)
        for drop_key in truncation_order:
            if estimate_tokens(body) <= budget.max_input_tokens:
                break
            if drop_key in remaining_sections:
                remaining_sections.remove(drop_key)
                body = _rebuild_body(
                    task_type=task_type,
                    generated_at=generated_at,
                    project=project,
                    agent_state=agent_state,
                    current_branch=current_branch,
                    gate_line=gate_line,
                    gate_result=gate_result,
                    gate_failure_summary=gate_failure_summary,
                    instruction_names=instruction_names,
                    mandate=mandate,
                    decisions=decisions,
                    questions=questions,
                    running_summary=running_summary,
                    git_status=git_status,
                    git_diff_stat=git_diff_stat,
                    diff_md=diff_md,
                    preflight_log_tail=preflight_log_tail,
                    included_keys=set(remaining_sections),
                )

        included_sections = remaining_sections

    final_estimated = estimate_tokens(body)

    if warnings:
        warning_block = "\n".join(f"- {w}" for w in warnings)
        body = body + f"\n## Warnings\n\n{warning_block}\n"

    return ContextPack(
        task_type=task_type,
        generated_at=generated_at,
        project_name=project.name,
        agent=str(agent_state.get("agent", "")),
        issue=agent_state.get("issue", ""),
        title=str(agent_state.get("title", "")),
        slug=str(agent_state.get("slug", "")),
        worktree=worktree_str,
        current_branch=current_branch,
        current_state=str(agent_state.get("state", "")),
        estimated_input_tokens=final_estimated,
        max_input_tokens=budget.max_input_tokens,
        truncated=truncated,
        included_sections=included_sections,
        warnings=warnings,
        body=body,
    )


def _rebuild_body(
    *,
    task_type: str,
    generated_at: str,
    project: ProjectConfig,
    agent_state: dict[str, object],
    current_branch: str,
    gate_line: str,
    gate_result: dict[str, object] | None,
    gate_failure_summary: str,
    instruction_names: list[str],
    mandate: str,
    decisions: str,
    questions: str,
    running_summary: str,
    git_status: str,
    git_diff_stat: str,
    diff_md: str,
    preflight_log_tail: str,
    included_keys: set[str],
) -> str:
    """Rebuild context pack body including only sections in included_keys."""
    worktree_str = str(agent_state.get("worktree", ""))
    sections: list[str] = []

    sections.append(
        f"# Cascade Context Pack\n\n"
        f"- Task: {task_type}\n"
        f"- Generated: {generated_at}\n"
        f"- Project: {project.name}\n"
        f"- Agent: {agent_state.get('agent', '')}\n"
        f"- Issue: #{agent_state.get('issue', '')}\n"
        f"- Title: {agent_state.get('title', '')}\n"
        f"- Slug: {agent_state.get('slug', '')}\n"
        f"- State: {agent_state.get('state', '')}\n"
        f"- Worktree: {worktree_str}\n"
        f"- Branch: {current_branch}\n"
    )
    sections.append(_operating_rules_block())

    gate_section = f"## Gate Result\n\n- Status: {gate_line}\n"
    if gate_result:
        gate_section += f"- Exit code: {gate_result.get('exit_code', '?')}\n"
        gate_section += f"- Log: {gate_result.get('log_path', '(unknown)')}\n"
    if gate_failure_summary:
        gate_section += f"\n### Failure summary\n\n{gate_failure_summary}\n"
    sections.append(gate_section)

    if "instruction_files" in included_keys and instruction_names:
        sections.append(
            "## Configured instruction files\n\n"
            + "\n".join(f"- {n}" for n in instruction_names)
            + "\n"
        )
    if "mandate" in included_keys and mandate:
        sections.append(f"## Mandate\n\n{mandate}\n")
    if "decisions" in included_keys and decisions:
        sections.append(f"## Decisions\n\n{decisions}\n")
    if "questions" in included_keys and questions:
        sections.append(f"## Questions\n\n{questions}\n")
    if "running_summary" in included_keys and running_summary:
        sections.append(f"## Running Summary\n\n{running_summary}\n")
    if "git_status" in included_keys and git_status:
        sections.append(f"## Git Status\n\n{git_status}\n")
    if "git_diff_stat" in included_keys and git_diff_stat:
        sections.append(f"## Git Diff Stat\n\n{git_diff_stat}\n")
    if "diff_md" in included_keys and diff_md:
        sections.append(f"## Saved Diff Summary\n\n{diff_md}\n")
    if "preflight_log_tail" in included_keys and preflight_log_tail:
        sections.append(f"## Preflight Log Tail\n\n{preflight_log_tail}\n")

    return "\n".join(sections)


def save_context_pack(run_dir: Path, pack: ContextPack) -> tuple[Path, Path]:
    """Save context pack markdown and JSON metadata files.

    Returns (md_path, json_path).
    """
    md_path = run_dir / f"context_{pack.task_type}.md"
    json_path = run_dir / f"context_{pack.task_type}.json"

    md_path.write_text(pack.body, encoding="utf-8")

    metadata: dict[str, object] = {
        "task_type": pack.task_type,
        "generated_at": pack.generated_at,
        "estimated_input_tokens": pack.estimated_input_tokens,
        "max_input_tokens": pack.max_input_tokens,
        "truncated": pack.truncated,
        "included_sections": pack.included_sections,
        "warnings": pack.warnings,
    }
    json_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    return md_path, json_path
