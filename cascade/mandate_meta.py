from __future__ import annotations

import json
from pathlib import Path

from cascade.config import ProjectConfig
from cascade.worktrees import slugify


def mandate_metadata_path(worktree: Path, slug: str) -> Path:
    return worktree / ".github" / "mandates" / f"{slug}.json"


def read_mandate_metadata(worktree: Path, slug: str) -> dict[str, object] | None:
    path = mandate_metadata_path(worktree, slug)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def read_mandate_id(worktree: Path, slug: str) -> str | None:
    payload = read_mandate_metadata(worktree, slug)
    if payload is None:
        return None
    mandate_id = payload.get("mandate_id")
    if not isinstance(mandate_id, str) or not mandate_id.strip():
        return None
    return mandate_id.strip()


def validate_mandate_metadata(
    *,
    worktree: Path,
    slug: str,
    agent: str,
    active_branch: str | None,
    project_config: ProjectConfig,
    repo_name: str,
    expected_worktree_path: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    payload = read_mandate_metadata(worktree, slug)
    meta_path = mandate_metadata_path(worktree, slug)
    if payload is None:
        return [f"Missing or invalid mandate metadata: {meta_path}"]

    expected_branch = (
        project_config.branches.agent_branch_template or "agent/{agent}/{slug}"
    ).format(agent=agent, slug=slug)

    branch_value = payload.get("agent_branch")
    if not isinstance(branch_value, str) or branch_value != expected_branch:
        errors.append(
            f"agent_branch mismatch: expected '{expected_branch}', found '{branch_value}'."
        )

    if active_branch:
        active_branch_value = payload.get("active_branch")
        if not isinstance(active_branch_value, str) or active_branch_value != active_branch:
            errors.append(
                f"active_branch mismatch: expected '{active_branch}', found '{active_branch_value}'."
            )

    mandate_id = payload.get("mandate_id")
    if not isinstance(mandate_id, str) or not mandate_id.strip():
        errors.append("mandate_id missing or empty.")

    canonical_mandate = payload.get("canonical_mandate")
    if not isinstance(canonical_mandate, str) or not canonical_mandate.strip():
        errors.append("canonical_mandate missing or empty.")

    worktree_path_value = payload.get("worktree_path")
    if expected_worktree_path is not None:
        if not isinstance(worktree_path_value, str) or Path(worktree_path_value).resolve() != expected_worktree_path.resolve():
            errors.append(
                "worktree_path mismatch: "
                f"expected '{expected_worktree_path}', found '{worktree_path_value}'."
            )

    repo_value = payload.get("repo")
    if not isinstance(repo_value, str) or repo_value.strip() != repo_name:
        errors.append(f"repo mismatch: expected '{repo_name}', found '{repo_value}'.")

    slug_value = payload.get("slug")
    if not isinstance(slug_value, str) or slugify(slug_value) != slug:
        errors.append(f"slug mismatch: expected '{slug}', found '{slug_value}'.")

    return errors
