from __future__ import annotations

import subprocess
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, cmd: str, cwd: Path | None, exit_code: int, output: str) -> None:
        location = str(cwd) if cwd is not None else "<current working directory>"
        message = (
            "Command failed.\n"
            f"Command: {cmd}\n"
            f"Cwd: {location}\n"
            f"Exit code: {exit_code}\n"
            f"Output:\n{output}"
        )
        super().__init__(message)
        self.cmd = cmd
        self.cwd = cwd
        self.exit_code = exit_code
        self.output = output


def run_command(cmd: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError(cmd=cmd, cwd=cwd, exit_code=result.returncode, output=result.stdout)
    return result