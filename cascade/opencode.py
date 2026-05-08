from __future__ import annotations

from dataclasses import dataclass
import shlex
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Callable

class OpenCodeMode(str, Enum):
    plan = "plan"
    build = "build"


class OpenCodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenCodeRunResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def mode_to_agent(mode: OpenCodeMode | None) -> str | None:
    if mode is None:
        return None
    return mode.value


def ensure_opencode_available() -> None:
    if shutil.which("opencode") is None:
        raise OpenCodeError("OpenCode CLI `opencode` is not installed or not on PATH.")


def supports_non_interactive_run() -> tuple[bool, str | None]:
    if shutil.which("opencode") is None:
        return False, "OpenCode CLI `opencode` is not installed or not on PATH."

    top_level_help = subprocess.run(
        ["opencode", "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if top_level_help.returncode != 0:
        return False, "`opencode --help` failed; cannot verify non-interactive support."

    help_text = f"{top_level_help.stdout}\n{top_level_help.stderr}".lower()
    if "run" not in help_text:
        return False, "OpenCode help does not list a `run` subcommand."

    run_help = subprocess.run(
        ["opencode", "run", "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if run_help.returncode != 0:
        return False, "`opencode run --help` failed; non-interactive run is unavailable."

    return True, None


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


def build_non_interactive_command(
    prompt: str,
    model: str,
    mode: OpenCodeMode | None = None,
    use_continue: bool = True,
) -> list[str]:
    command = ["opencode", "run", "--model", model]
    if use_continue:
        command.append("--continue")
    agent_mode = mode_to_agent(mode)
    if agent_mode is not None:
        command.extend(["--agent", agent_mode])
    command.append(prompt)
    return command


def run_prompt_with_result(
    prompt: str,
    worktree: Path,
    model: str,
    mode: OpenCodeMode | None = None,
    use_continue: bool = True,
) -> OpenCodeRunResult:
    command = build_non_interactive_command(
        prompt=prompt,
        model=model,
        mode=mode,
        use_continue=use_continue,
    )
    result = subprocess.run(
        command,
        cwd=worktree,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return OpenCodeRunResult(
        command=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_prompt_streaming(
    prompt: str,
    worktree: Path,
    model: str,
    *,
    mode: OpenCodeMode | None = None,
    use_continue: bool = True,
    log_path: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> OpenCodeRunResult:
    command = build_non_interactive_command(
        prompt=prompt,
        model=model,
        mode=mode,
        use_continue=use_continue,
    )

    output_parts: list[str] = []
    log_file = None
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("w", encoding="utf-8")

        process = subprocess.Popen(
            command,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        if log_file is not None:
            log_file.close()
        raise OpenCodeError(f"OpenCode command failed to start: {exc}") from exc

    if process.stdout is None:
        process.wait()
        if log_file is not None:
            log_file.close()
        raise OpenCodeError("OpenCode streaming output is unavailable in this environment.")

    for line in process.stdout:
        output_parts.append(line)
        if log_file is not None:
            log_file.write(line)
            log_file.flush()
        if on_line is not None:
            on_line(line.rstrip("\n"))

    returncode = process.wait()
    if log_file is not None:
        log_file.close()

    return OpenCodeRunResult(
        command=command,
        returncode=returncode,
        stdout="".join(output_parts),
        stderr="",
    )


def run_prompt(
    prompt: str,
    worktree: Path,
    model: str,
    mode: OpenCodeMode | None = None,
    use_continue: bool = True,
) -> str:
    try:
        result = run_prompt_with_result(
            prompt=prompt,
            worktree=worktree,
            model=model,
            mode=mode,
            use_continue=use_continue,
        )
    except OSError as exc:
        raise OpenCodeError(f"OpenCode command failed to start: {exc}") from exc

    if result.returncode != 0:
        rendered_command = " ".join(shlex.quote(token) for token in result.command)
        raise OpenCodeError(
            "OpenCode command failed.\n"
            f"Command: {rendered_command}\n"
            f"Exit code: {result.returncode}\n"
            "Output:\n"
            f"{result.stdout}{result.stderr}\n"
            "If this looks like an unsupported flag, run `opencode --help`."
        )

    return result.stdout
