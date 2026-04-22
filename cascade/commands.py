from __future__ import annotations

from typing import TypedDict


class CommandMeta(TypedDict):
    description: str
    requires_opencode: bool
    requires_gh: bool
    mutates_target_repo: bool


NO_MODEL_COMMANDS: dict[str, CommandMeta] = {
    "doctor": {
        "description": "Check local prerequisites and project config.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "claim": {
        "description": "Claim a GitHub issue and create a configured agent worktree.",
        "requires_opencode": False,
        "requires_gh": True,
        "mutates_target_repo": True,
    },
    "status": {
        "description": "Show agent claim status from local state.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "show-prompt": {
        "description": "Print the saved launch prompt for an agent.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "mark": {
        "description": "Update lifecycle state in local agent state.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "note": {
        "description": "Record a deterministic user decision note.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "context": {
        "description": "Generate deterministic consolidated context for an agent run.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "diff": {
        "description": "Show deterministic git status and diff summary.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "logs": {
        "description": "Print run artifacts such as preflight or mandate files.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "preflight": {
        "description": "Run configured preflight validation command and persist log.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": True,
    },
    "capabilities": {
        "description": "List command categories and required capabilities.",
        "requires_opencode": False,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
}


MODEL_BACKED_COMMANDS: dict[str, CommandMeta] = {
    "run-agent": {
        "description": "Launch interactive OpenCode session for an agent.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": True,
    },
    "chat": {
        "description": "Launch interactive OpenCode session for this agent.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": True,
    },
    "ask": {
        "description": "Ask the model a question through OpenCode.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "summarize": {
        "description": "Ask the model to summarize current work.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "continue": {
        "description": "Prepare continuation prompt and launch OpenCode.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": True,
    },
}


PLANNED_MODEL_BACKED_COMMANDS: dict[str, CommandMeta] = {
    "plan": {
        "description": "Ask a model to produce an implementation plan.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "implement": {
        "description": "Ask a model to implement approved work items.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": True,
    },
    "diagnose": {
        "description": "Ask a model to interpret failed validation logs.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
    "fix": {
        "description": "Ask a model to propose and apply the smallest safe fix.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": True,
    },
    "review": {
        "description": "Ask a model to review a prepared change set.",
        "requires_opencode": True,
        "requires_gh": False,
        "mutates_target_repo": False,
    },
}
