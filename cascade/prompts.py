from __future__ import annotations

from pathlib import Path

from cascade.config import ProjectConfig


def build_launch_prompt(
    project: ProjectConfig,
    agent_state: dict[str, object],
    mandate_body: str,
    instruction_files: list[Path],
) -> str:
    instruction_lines = "\n".join(f"- {path}" for path in instruction_files)
    if not instruction_lines:
        instruction_lines = "- No configured instruction files"

    return f"""You are Cascade Agent `{agent_state['agent']}` working on project `{project.name}`.

You are operating inside this worktree:

`{agent_state['worktree']}`

You are implementing GitHub issue #{agent_state['issue']}: {agent_state['title']}

# Mandate

{mandate_body}

# Required repo instructions

Before editing files, read and obey these files if present:

{instruction_lines}

# Operating rules

- Work only inside the assigned worktree.
- Do not modify unrelated worktrees.
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

Start by reading the instruction files and summarizing:
1. The repo rules.
2. The files/areas you expect to touch.
3. Any clarifying questions.
"""