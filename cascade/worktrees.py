from __future__ import annotations

import re
from pathlib import Path

from cascade.config import ProjectConfig


def slugify(value: str, max_length: int = 80) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"[\u2013\u2014\s]+", "-", normalized)
    normalized = re.sub(r"[^a-z0-9_-]+", "", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized.strip("-")
    if not normalized:
        return "untitled"
    return normalized[:max_length].rstrip("-")


def resolve_worktree_path(project: ProjectConfig, agent: str, slug: str) -> Path:
    return project.paths.worktree_root / f"{agent}-{slug}"


def find_worktree_path(project: ProjectConfig, agent: str, slug: str) -> tuple[Path, str | None]:
    convention_path = resolve_worktree_path(project, agent, slug)
    worktree_root = project.paths.worktree_root
    candidates = [
        convention_path,
        worktree_root / f"{agent}_{slug}",
        worktree_root / slug,
    ]

    if worktree_root.exists():
        for entry in sorted(worktree_root.iterdir()):
            if entry.is_dir() and agent in entry.name and slug in entry.name:
                candidates.append(entry)

    existing_candidates = _dedupe_existing_paths(candidates)
    if len(existing_candidates) == 1:
        return existing_candidates[0], None
    if len(existing_candidates) == 0:
        return convention_path, (
            "Could not find a matching worktree directory after create_worktree succeeded. "
            "Using the convention-based path."
        )
    return convention_path, (
        "Found multiple possible worktree directories after create_worktree succeeded. "
        "Using the convention-based path."
    )


def _dedupe_existing_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped