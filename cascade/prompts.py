from __future__ import annotations

from pathlib import Path

from cascade.config import ProjectConfig, resolve_workspace_root


def _workspace_boundary_block(project: ProjectConfig) -> str:
    """Build the workspace boundary section for model prompts."""
    workspace = resolve_workspace_root(project)
    if workspace is None:
        return ""
    lines = [
        "# Workspace boundary",
        "",
        f"- workspace_root: {workspace}",
        f"- repo_root: {project.paths.repo_root}",
        f"- worktree_root: {project.paths.worktree_root}",
    ]
    if project.paths.secrets_root is not None:
        lines.append(f"- secrets_root: {project.paths.secrets_root}")
    for name, rpath in project.related_repos.items():
        lines.append(f"- related_repos.{name}: {rpath}")
    lines += [
        "",
        "Only operate inside the assigned worktree and explicitly declared project paths.",
        "Do not inspect or edit unrelated sibling repositories in the workspace.",
        "",
    ]
    return "\n".join(lines) + "\n"


def build_launch_prompt(
    project: ProjectConfig,
    agent_state: dict[str, object],
    mandate_body: str,
    instruction_files: list[Path],
) -> str:
    instruction_lines = "\n".join(f"- {path}" for path in instruction_files)
    if not instruction_lines:
        instruction_lines = "- No configured instruction files"

    workspace_block = _workspace_boundary_block(project)

    return f"""You are Cascade Agent `{agent_state['agent']}` working on project `{project.name}`.

You are operating inside this worktree:

`{agent_state['worktree']}`

You are implementing GitHub issue #{agent_state['issue']}: {agent_state['title']}

# Mandate

{mandate_body}

# Required repo instructions

Before editing files, read and obey these files if present:

{instruction_lines}

{workspace_block}# Operating rules

- Work only inside the assigned worktree.
- Do not modify unrelated worktrees.
- Do not inspect or edit unrelated sibling repositories in the workspace.
- Use the project's configured commands.
- Prefer repo Make targets over ad-hoc commands.
- Do not run destructive cleanup or removal commands unless explicitly told.
- Do not weaken tests, coverage, typing, pre-commit, pre-push, mandate gates, or CI gates.
- Do not edit pre-commit, pre-push, mandate gate, or enforcement code unless explicitly authorized.
- Do not stage, commit, or push unless explicitly authorized.
- Do not treat model output as proof of validation; only configured command exit codes count.
- Ask clarifying questions before implementation if the mandate is ambiguous.
- Keep a concise running summary of decisions and changes.
- Before closeout, run the configured preflight command.
- If preflight fails, summarize the failure and propose the smallest safe fix.

# Output discipline

- Do not narrate excessively or pad responses.
- Summarize the files changed and the next validation command after each edit batch.
- Do not refactor unrelated code.
- Do not weaken gates.
- Do not stage, commit, or push.

Start by reading the instruction files and summarizing:
1. The repo rules.
2. The files/areas you expect to touch.
3. Any clarifying questions.
"""


# ---------------------------------------------------------------------------
# Task-specific output discipline blocks
# ---------------------------------------------------------------------------

_DIAGNOSE_OUTPUT_RULES = """\
# Output discipline for diagnosis

Respond with:
1. Failed gate/hook name
2. Likely root cause (one sentence)
3. Smallest safe fix (file and line if possible)
4. Files to inspect or change
5. Whether model-assisted fixing is recommended
6. Do not exceed 300 words unless a code patch is needed.
Do not claim the gate passed. Only command exit codes determine pass/fail.
"""

_FIX_OUTPUT_RULES = """\
# Output discipline for fix

- Address only the specified failure; do not refactor unrelated code.
- State which files you will change before editing.
- After each edit, summarize what changed and what to rerun.
- Do not weaken gates, tests, or coverage.
- Do not stage, commit, or push.
- Do not exceed 400 words unless a code patch is included.
"""

_REVIEW_OUTPUT_RULES = """\
# Output discipline for review

Respond with:
1. Risk summary (one paragraph)
2. Mandate compliance (pass/fail with reason)
3. Gate compliance (pass/fail with reason — determined by configured commands, not by this review)
4. Files touched
5. Missing tests or coverage risks
6. Approval recommendation
7. Do not exceed 500 words.
"""

_IMPLEMENT_OUTPUT_RULES = """\
# Output discipline for implementation

- Ask blocking questions first if the mandate is unclear.
- Do not narrate excessively.
- Summarize files changed and the next validation command after each edit batch.
- Do not refactor unrelated code.
- Do not weaken gates.
- Do not stage, commit, or push.
"""

_PLAN_OUTPUT_RULES = """\
# Output discipline for planning

- Produce a structured plan with numbered steps.
- Identify files and modules to change.
- List clarifying questions before committing to the plan.
- Do not start editing until the plan is approved.
- Do not exceed 600 words for the plan body.
"""

_TASK_OUTPUT_RULES: dict[str, str] = {
    "diagnose": _DIAGNOSE_OUTPUT_RULES,
    "fix": _FIX_OUTPUT_RULES,
    "review": _REVIEW_OUTPUT_RULES,
    "implement": _IMPLEMENT_OUTPUT_RULES,
    "plan": _PLAN_OUTPUT_RULES,
}


def get_task_output_rules(task_type: str) -> str:
    """Return the output discipline block for a given task type."""
    return _TASK_OUTPUT_RULES.get(task_type, "")


def build_task_prompt(context_pack_body: str, task_type: str) -> str:
    """Build a model prompt from a context pack body and task-specific rules.

    The context pack body is prepended to the task instruction and output rules.
    This is used by `cascade prepare-model-call`.
    """
    output_rules = get_task_output_rules(task_type)
    task_instruction = _task_instruction(task_type)

    parts: list[str] = [context_pack_body]
    if task_instruction:
        parts.append(f"\n---\n\n# Task: {task_type}\n\n{task_instruction}")
    if output_rules:
        parts.append(f"\n{output_rules}")
    parts.append(
        "\n---\n\nReminder: Do not claim validation passed. "
        "Only configured command exit codes (e.g. `make preflight`) determine pass/fail.\n"
    )
    return "\n".join(parts)


def _task_instruction(task_type: str) -> str:
    instructions: dict[str, str] = {
        "diagnose": (
            "Review the gate failure log and context above. "
            "Identify the root cause and suggest the smallest safe fix."
        ),
        "fix": (
            "Apply the smallest safe fix for the gate failure described above. "
            "Touch only files required to resolve the specific failure."
        ),
        "review": (
            "Review the changes described in the context above against the mandate, "
            "gate results, and repo standards."
        ),
        "implement": (
            "Implement the mandate described above. "
            "Follow repo instructions. Ask blocking questions before editing."
        ),
        "plan": (
            "Produce a detailed implementation plan for the mandate above. "
            "List all files to change and clarifying questions."
        ),
        "summarize": (
            "Summarize the current state of work: what has been done, what remains, "
            "and any open questions."
        ),
    }
    return instructions.get(task_type, f"Perform the '{task_type}' task described in the context.")
