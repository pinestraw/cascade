from __future__ import annotations

from pathlib import Path

from cascade.config import ProjectConfig

_DOCKER_PREFIX = "/workspace/"


def _is_docker_path(value: object) -> bool:
    return isinstance(value, str) and (value == "/workspace" or value.startswith(_DOCKER_PREFIX))


def detect_docker_era_state(agent_state: dict[str, object]) -> list[str]:
    stale_keys: list[str] = []
    for key in ("project_file", "worktree", "run_dir", "repo_root", "secrets_root"):
        if _is_docker_path(agent_state.get(key)):
            stale_keys.append(key)
    return stale_keys


def _remap_path(raw_value: str, *, project_config: ProjectConfig, agent: str) -> str:
    if not raw_value.startswith(_DOCKER_PREFIX):
        return raw_value

    if raw_value.startswith("/workspace/jungle-worktrees/"):
        suffix = raw_value.removeprefix("/workspace/jungle-worktrees/")
        return str((project_config.paths.worktree_root / suffix).resolve())

    if raw_value.startswith("/workspace/jungle-secrets/") and project_config.paths.secrets_root is not None:
        suffix = raw_value.removeprefix("/workspace/jungle-secrets/")
        return str((project_config.paths.secrets_root / suffix).resolve())

    if raw_value == "/workspace/jungle-secrets" and project_config.paths.secrets_root is not None:
        return str(project_config.paths.secrets_root.resolve())

    if raw_value == "/workspace/jungle":
        return str(project_config.paths.repo_root.resolve())

    if raw_value.startswith("/workspace/jungle/"):
        suffix = raw_value.removeprefix("/workspace/jungle/")
        return str((project_config.paths.repo_root / suffix).resolve())

    if raw_value.startswith("/workspace/cascade/state/"):
        # state/<project>/runs/<agent>
        suffix = raw_value.removeprefix("/workspace/cascade/state/")
        return str((Path.cwd() / "state" / suffix).resolve())

    if raw_value.startswith("/workspace/cascade/examples/"):
        suffix = raw_value.removeprefix("/workspace/cascade/")
        return str((Path.cwd() / suffix).resolve())

    if raw_value.startswith("/workspace/cascade/"):
        suffix = raw_value.removeprefix("/workspace/cascade/")
        return str((Path.cwd() / suffix).resolve())

    return raw_value


def migrate_docker_era_state(
    agent_state: dict[str, object],
    *,
    project_config: ProjectConfig,
) -> tuple[dict[str, object], list[str]]:
    migrated = dict(agent_state)
    changes: list[str] = []
    agent = str(agent_state.get("agent") or "")

    for key in ("project_file", "worktree", "run_dir", "repo_root", "secrets_root"):
        value = migrated.get(key)
        if not isinstance(value, str):
            continue
        new_value = _remap_path(value, project_config=project_config, agent=agent)
        if new_value != value:
            migrated[key] = new_value
            changes.append(f"{key}: {value} -> {new_value}")

    return migrated, changes
