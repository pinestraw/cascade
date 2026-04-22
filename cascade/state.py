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