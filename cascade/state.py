from __future__ import annotations

import json
from pathlib import Path


STATE_DIR_NAME = "state"


def get_project_state_dir(project_name: str) -> Path:
    return Path.cwd() / STATE_DIR_NAME / project_name


def get_project_agents_dir(project_name: str) -> Path:
    return get_project_state_dir(project_name) / "agents"


def get_project_runs_dir(project_name: str) -> Path:
    return get_project_state_dir(project_name) / "runs"


def get_agent_run_dir(project_name: str, agent: str) -> Path:
    return get_project_runs_dir(project_name) / agent


def get_agent_state_path(project_name: str, agent: str) -> Path:
    return get_project_agents_dir(project_name) / f"{agent}.json"


def ensure_project_state_dirs(project_name: str, agent: str) -> tuple[Path, Path]:
    agents_dir = get_project_agents_dir(project_name)
    run_dir = get_agent_run_dir(project_name, agent)
    agents_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir, run_dir


def save_agent_state(project_name: str, agent: str, state: dict[str, object]) -> None:
    state_path = get_agent_state_path(project_name, agent)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def load_agent_state(project_name: str, agent: str) -> dict[str, object]:
    state_path = get_agent_state_path(project_name, agent)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Agent state not found: {state_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Invalid agent state format: {state_path}")
    return payload


def list_agent_states(project_name: str) -> list[dict[str, object]]:
    agents_dir = get_project_agents_dir(project_name)
    if not agents_dir.exists():
        return []

    states: list[dict[str, object]] = []
    for state_file in sorted(agents_dir.glob("*.json")):
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            states.append(payload)
    return states


def update_agent_state(project_name: str, agent: str, new_state: str) -> dict[str, object]:
    state = load_agent_state(project_name, agent)
    state["state"] = new_state
    save_agent_state(project_name, agent, state)
    return state


# ---------------------------------------------------------------------------
# Retry / attempt tracking
# ---------------------------------------------------------------------------

_TRACKED_TASK_TYPES = ("plan", "implement", "diagnose", "fix", "review", "summarize")


def _ensure_attempts(agent_state: dict[str, object]) -> dict[str, object]:
    """Return the attempts sub-dict, creating it if absent."""
    if not isinstance(agent_state.get("attempts"), dict):
        agent_state["attempts"] = {
            task: {"count": 0, "last_profile": None}
            for task in _TRACKED_TASK_TYPES
        }
    attempts = agent_state["attempts"]
    assert isinstance(attempts, dict)
    for task in _TRACKED_TASK_TYPES:
        if task not in attempts:
            attempts[task] = {"count": 0, "last_profile": None}
    return attempts


def increment_attempt(
    project_name: str,
    agent: str,
    task_type: str,
    profile: str | None = None,
) -> int:
    """Increment attempt count for task_type; return the new count."""
    state = load_agent_state(project_name, agent)
    attempts = _ensure_attempts(state)
    task_entry = attempts.get(task_type)
    if not isinstance(task_entry, dict):
        task_entry = {"count": 0, "last_profile": None}
        attempts[task_type] = task_entry
    task_entry["count"] = int(task_entry.get("count", 0)) + 1
    task_entry["last_profile"] = profile
    save_agent_state(project_name, agent, state)
    return int(task_entry["count"])


def get_attempt_count(project_name: str, agent: str, task_type: str) -> int:
    """Return the current attempt count for task_type (0 if not started)."""
    try:
        state = load_agent_state(project_name, agent)
    except (FileNotFoundError, ValueError):
        return 0
    attempts = state.get("attempts")
    if not isinstance(attempts, dict):
        return 0
    task_entry = attempts.get(task_type)
    if not isinstance(task_entry, dict):
        return 0
    return int(task_entry.get("count", 0))


def should_escalate(
    project_config: object,
    agent_state: dict[str, object],
    task_type: str,
) -> bool:
    """Return True if the attempt count has reached the escalation threshold.

    Requires project_config to have a retry_policy attribute (ProjectConfig).
    Falls back gracefully if the attribute is missing.
    """
    from cascade.config import ProjectConfig  # avoid circular at module top

    if not isinstance(project_config, ProjectConfig):
        return False

    attempts = agent_state.get("attempts")
    if not isinstance(attempts, dict):
        return False
    task_entry = attempts.get(task_type)
    if not isinstance(task_entry, dict):
        return False

    count = int(task_entry.get("count", 0))
    last_profile = str(task_entry.get("last_profile") or "")
    threshold = project_config.retry_policy.max_attempts_for_profile(last_profile)
    return count >= threshold
