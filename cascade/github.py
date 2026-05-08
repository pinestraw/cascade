from __future__ import annotations

import json
import os
from pathlib import Path
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


def read_project_config(worktree: Path) -> dict[str, object] | None:
    path = worktree / ".github" / "mandates" / ".project-config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _gh_graphql(query: str, *, token: str | None = None) -> dict[str, object] | None:
    if shutil.which("gh") is None:
        return None

    chosen_token = token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not chosen_token:
        return None

    cmd = f"GH_TOKEN={shlex.quote(chosen_token)} gh api graphql -f query={shlex.quote(query)}"
    try:
        result = run_command(cmd)
    except CommandError:
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def get_project_item_for_issue(
    *,
    owner: str,
    repo: str,
    project_number: int,
    issue_number: int,
    token: str | None = None,
) -> dict[str, object] | None:
    query = (
        "query {"
        f" repository(owner: \"{owner}\", name: \"{repo}\") {{"
        f"  issue(number: {issue_number}) {{ id number }}"
        " }"
        f" organization(login: \"{owner}\") {{"
        f"  projectV2(number: {project_number}) {{"
        "   id"
        "   items(first: 100) {"
        "    nodes {"
        "     id"
        "     content { ... on Issue { id number } }"
        "    }"
        "   }"
        "  }"
        " }"
        "}"
    )
    payload = _gh_graphql(query, token=token)
    if payload is None:
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    org_data = data.get("organization")
    if not isinstance(org_data, dict):
        return None
    project_data = org_data.get("projectV2")
    if not isinstance(project_data, dict):
        return None
    items = project_data.get("items")
    if not isinstance(items, dict):
        return None
    nodes = items.get("nodes")
    if not isinstance(nodes, list):
        return None

    for node in nodes:
        if not isinstance(node, dict):
            continue
        content = node.get("content")
        if not isinstance(content, dict):
            continue
        number = content.get("number")
        if number == issue_number:
            return {
                "item_id": node.get("id"),
                "project_id": project_data.get("id"),
            }
    return None


def update_project_v2_item_status(
    *,
    project_id: str,
    item_id: str,
    field_id: str,
    option_id: str,
    token: str | None = None,
) -> bool:
    mutation = (
        "mutation {"
        " updateProjectV2ItemFieldValue(input: {"
        f"  projectId: \"{project_id}\""
        f"  itemId: \"{item_id}\""
        f"  fieldId: \"{field_id}\""
        f"  value: {{ singleSelectOptionId: \"{option_id}\" }}"
        " }) {"
        "  projectV2Item { id }"
        " }"
        "}"
    )
    payload = _gh_graphql(mutation, token=token)
    if payload is None:
        return False
    return "errors" not in payload


def update_project_v2_text_field(
    *,
    project_id: str,
    item_id: str,
    field_id: str,
    value: str,
    token: str | None = None,
) -> bool:
    mutation = (
        "mutation {"
        " updateProjectV2ItemFieldValue(input: {"
        f"  projectId: \"{project_id}\""
        f"  itemId: \"{item_id}\""
        f"  fieldId: \"{field_id}\""
        f"  value: {{ text: \"{value}\" }}"
        " }) {"
        "  projectV2Item { id }"
        " }"
        "}"
    )
    payload = _gh_graphql(mutation, token=token)
    if payload is None:
        return False
    return "errors" not in payload