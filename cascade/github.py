from __future__ import annotations

import json
import shlex
import shutil

from cascade.shell import CommandError, run_command


class GithubError(RuntimeError):
    pass


def fetch_issue(owner: str, repo: str, issue: int) -> dict[str, object]:
    if shutil.which("gh") is None:
        raise GithubError("GitHub CLI `gh` is not installed or not on PATH.")

    cmd = (
        "gh issue view "
        f"{shlex.quote(str(issue))} "
        f"--repo {shlex.quote(f'{owner}/{repo}')} "
        "--json title,body,number"
    )
    try:
        result = run_command(cmd)
    except CommandError as exc:
        raise GithubError(str(exc)) from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GithubError(f"Failed to parse GitHub CLI output as JSON:\n{result.stdout}") from exc

    if not isinstance(payload, dict):
        raise GithubError("Unexpected GitHub CLI response format.")
    return payload