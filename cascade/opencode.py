from __future__ import annotations

import shlex
import shutil
from enum import Enum
from pathlib import Path

from cascade.shell import CommandError, run_command


class OpenCodeMode(str, Enum):
    plan = "plan"
    build = "build"


class OpenCodeError(RuntimeError):
    pass


def mode_to_agent(mode: OpenCodeMode | None) -> str | None:
    if mode is None:
        return None
    return mode.value


def ensure_opencode_available() -> None:
    if shutil.which("opencode") is None:
        raise OpenCodeError("OpenCode CLI `opencode` is not installed or not on PATH.")


def build_interactive_command(
    model: str,
    mode: OpenCodeMode | None = None,
    prompt: str | None = None,
) -> list[str]:
    command = ["opencode", ".", "--model", model]
    agent_mode = mode_to_agent(mode)
    if agent_mode is not None:
        command.extend(["--agent", agent_mode])
    if prompt is not None:
        command.extend(["--prompt", prompt])
    return command


def run_prompt(
    prompt: str,
    worktree: Path,
    model: str,
    mode: OpenCodeMode | None = None,
    use_continue: bool = True,
) -> str:
    quoted_prompt = shlex.quote(prompt)
    command_parts = ["opencode", "run", "--model", shlex.quote(model)]
    if use_continue:
        command_parts.append("--continue")
    agent_mode = mode_to_agent(mode)
    if agent_mode is not None:
        command_parts.extend(["--agent", shlex.quote(agent_mode)])
    command_parts.append(quoted_prompt)
    command = " ".join(command_parts)

    try:
        result = run_command(command, cwd=worktree)
    except CommandError as exc:
        raise OpenCodeError(
            f"OpenCode command failed.\n{exc}\nIf this looks like an unsupported flag, run `opencode --help`."
        ) from exc
    return result.stdout
