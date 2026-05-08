from __future__ import annotations

import builtins
from dataclasses import dataclass
import fnmatch
import os
import platform
import json
import queue
import re
import shlex
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from cascade.config import (
    ConfigError,
    ModelProfile,
    ProjectConfig,
    get_model_profile,
    is_inside_workspace,
    instruction_file_paths,
    load_project_config,
    model_id_for_opencode,
    resolve_workspace_link_paths,
    resolve_workspace_root,
    resolve_model_for_task,
    get_default_gate_fix_model,
    get_gate_fix_fallback_models,
    resolve_gate_fix_model_profile,
)
from cascade.commands import MODEL_BACKED_COMMANDS, NO_MODEL_COMMANDS, PLANNED_MODEL_BACKED_COMMANDS
from cascade.context_pack import ALLOWED_TASKS, build_context_pack, save_context_pack
from cascade.costs import DEFAULT_EXPECTED_OUTPUT_TOKENS, cost_summary_lines, estimate_cost, estimate_tokens
from cascade.prompts import build_launch_prompt, build_task_prompt
from cascade.conversation import (
    append_markdown_entry,
    build_ask_prompt,
    build_continue_prompt,
    build_summarize_prompt,
    ensure_conversation_files,
    read_tail_chars,
    read_text,
    timestamp_utc,
)
from cascade.doctor import has_failures, run_doctor_checks
from cascade.github import GithubError, fetch_issue
from cascade.github import (
    get_project_item_for_issue,
    read_project_config as read_github_project_config,
    update_project_v2_item_status,
    update_project_v2_text_field,
)
from cascade.lifecycle import AgentLifecycleState
from cascade.mandate_meta import read_mandate_id, read_mandate_metadata, validate_mandate_metadata
from cascade.migration import detect_docker_era_state, migrate_docker_era_state
from cascade.opencode import (
    OpenCodeError,
    OpenCodeMode,
    build_interactive_command,
    ensure_opencode_available,
    run_prompt_streaming,
    run_prompt_with_result,
    run_prompt,
    supports_non_interactive_run,
)
from cascade.opencode_setup import ensure_opencode_host_path_setup
from cascade.shell import CommandError, run_command
from cascade.state import (
    ensure_project_state_dirs,
    get_agent_run_dir,
    increment_attempt,
    get_attempt_count,
    load_agent_state,
    save_agent_state,
    list_agent_states,
    update_agent_state,
)
from cascade.gates import (
    build_failure_summary,
    check_gate_staleness,
    classify_gate_failure,
    failure_signature,
    get_diff_fingerprint,
    get_git_head_sha,
    get_touched_files,
    gate_status_line,
    load_gate_result,
    save_gate_result,
)
from cascade.gate_fix import (
    GateFixBatchMode,
    GateFixConfig,
    GateFixCategory,
    classify_failure_as_model_fixable,
    is_model_fixable,
    run_gate_fix_loop,
    save_gate_fix_summary,
)
from cascade.standards import (
    get_current_branch,
    get_git_diff_names,
    get_git_diff_stat,
    get_git_status,
    validate_agent_branch,
    validate_instruction_files,
    validate_worktree_location,
)
from cascade.worktrees import find_worktree_path, slugify


console = Console()
app = typer.Typer(help="Cascade multi-agent mandate runner.")
NO_INIT_MANDATE_MESSAGE = (
    "Missing mandate start command config: set one of "
    "commands.mandate_start, commands.start_mandate, or commands.init_mandate."
)

_AUTOLOAD_ENV_KEYS = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DOCKER_BUILDKIT",
    "COMPOSE_DOCKER_CLI_BUILD",
}


class LogKind(str, Enum):
    preflight = "preflight"
    prompt = "prompt"
    mandate = "mandate"


class RepairKind(str, Enum):
    auto = "auto"
    missing_mandate_metadata = "missing-mandate-metadata"
    mandate_metadata = "mandate-metadata"
    missing_workspace_link = "missing-workspace-link"
    dirty_file = "dirty-file"
    closeout_dirty_file_prep = "closeout-dirty-file-prep"
    docker_era_state = "docker-era-state"
    docker_runtime_network = "docker-runtime-network"


@dataclass(frozen=True)
class RepairFinding:
    kind: RepairKind
    slug: str
    title: str
    worktree: Path
    metadata_path: Path
    canonical_mandate_path: Path
    message: str
    can_repair: bool
    repair_command: str | None


@dataclass(frozen=True)
class RepairResult:
    kind: RepairKind
    success: bool
    dry_run: bool
    message: str
    log_path: Path
    stash_ref: str | None = None
    stash_message: str | None = None
    stash_pop_conflict: bool = False


@dataclass(frozen=True)
class CloseoutPrepReport:
    status_lines: list[str]
    dirty_paths: list[str]
    mandate_owned_paths: list[str]
    suspicious_paths: list[str]
    metadata_paths: list[str]
    staged_paths: list[str]
    commit_message: str | None
    commit_performed: bool


@dataclass(frozen=True)
class LoopRunOptions:
    max_iterations: int | None
    max_model_fixes: int | None
    max_estimated_cost_usd: float | None
    dry_run: bool
    non_interactive: bool | None
    verbose: bool
    watch: bool
    profile: str | None
    cheap_profile: str
    debug_profile: str
    executor_profile: str
    stop_on_same_failure_twice: bool | None


@dataclass(frozen=True)
class PreflightRunResult:
    returncode: int
    output: str


_PREFLIGHT_PROGRESS_INTERVAL_SECONDS = 15.0
_PREFLIGHT_VERBOSE_TAIL_LINES = 5
_PREFLIGHT_FAILURE_TAIL_LINES = 20

_CURRENT_FAILURE_CONTEXT_FILENAME = "current_failure_context.json"
_COMMIT_FAILURE_CONTEXT_FILENAME = "closeout_prep_commit_failure.json"
_CLOSEOUT_FAILURE_CONTEXT_FILENAME = "closeout_failure_context.json"
_COMPLETE_LOOP_FILENAME = "complete_loop.json"
_DEFAULT_COMPLETE_PHASES = (
    "preflight",
    "closeout_prep_stage",
    "mandate_commit",
    "post_commit_preflight",
    "finish",
    "closeout",
)
_LEGACY_COMPLETE_PHASE_ALIASES = {
    "commit": "mandate_commit",
}


_DEFAULT_REPAIR_ROUTING: dict[str, dict[str, str]] = {
    "formatting": {"strategy": "deterministic_first", "profile": "cheap_coder"},
    "typing": {"strategy": "model", "profile": "debugger"},
    "coverage": {"strategy": "diagnose_then_fix", "profile": "debugger"},
    "migration": {"strategy": "stop_or_model_with_warning", "profile": "debugger"},
    "security": {"strategy": "stop_requires_approval", "profile": "debugger"},
    "policy": {"strategy": "stop_requires_approval", "profile": "debugger"},
    "unknown": {"strategy": "diagnose_only", "profile": "debugger"},
}


def _repair_loop_metadata_path(project: str, agent: str) -> Path:
    return get_agent_run_dir(project, agent) / "repair_loop.json"


def _complete_loop_metadata_path(project: str, agent: str) -> Path:
    return get_agent_run_dir(project, agent) / _COMPLETE_LOOP_FILENAME


def _write_json_artifact(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _load_json_artifact(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _git_status_entries(worktree: Path) -> list[tuple[str, str]]:
    try:
        status_output = run_command("git status --porcelain", cwd=worktree).stdout
    except CommandError:
        return []
    return _parse_git_status_porcelain(status_output)


def _worktree_has_dirty_changes(worktree: Path) -> bool:
    return bool(_git_status_entries(worktree))


def _append_completed_phase(payload: dict[str, object], phase: str) -> None:
    completed = payload.get("completed_phases")
    if not isinstance(completed, list):
        completed = []
    if phase not in completed:
        completed.append(phase)
    payload["completed_phases"] = completed


def _resolve_complete_start_phase(
    *,
    agent_state: dict[str, object],
    existing_metadata: dict[str, object] | None,
) -> str | None:
    if existing_metadata is not None:
        if str(existing_metadata.get("status") or "") == "completed":
            return None
        next_phase = str(existing_metadata.get("next_phase") or "").strip()
        next_phase = _LEGACY_COMPLETE_PHASE_ALIASES.get(next_phase, next_phase)
        if next_phase in _DEFAULT_COMPLETE_PHASES:
            return next_phase
        current_phase = str(existing_metadata.get("current_phase") or "").strip()
        current_phase = _LEGACY_COMPLETE_PHASE_ALIASES.get(current_phase, current_phase)
        if current_phase in _DEFAULT_COMPLETE_PHASES:
            return current_phase

    state_value = str(agent_state.get("state") or "")
    if state_value == AgentLifecycleState.closed.value:
        return None
    if state_value in {
        AgentLifecycleState.closeout_ready.value,
        AgentLifecycleState.closing_out.value,
        AgentLifecycleState.closeout_failed.value,
    }:
        return "closeout"
    if state_value == AgentLifecycleState.preflight_passed.value:
        return "mandate_commit"
    return "preflight"


def _is_dirty_file_commit_required_failure(result_or_log: object) -> bool:
    fragments: list[str] = []
    if isinstance(result_or_log, dict):
        for key in ("stop_reason", "last_action", "next_action", "next_command"):
            value = result_or_log.get(key)
            if isinstance(value, str) and value.strip():
                fragments.append(value)
        try:
            fragments.append(json.dumps(result_or_log))
        except TypeError:
            fragments.append(str(result_or_log))
    elif isinstance(result_or_log, str):
        fragments.append(result_or_log)
    else:
        fragments.append(str(result_or_log))

    haystack = "\n".join(fragments).lower()
    return (
        "dirty_file_commit_required" in haystack
        or "unexpected dirty file while closing mandate" in haystack
    )


def _initialize_complete_metadata(
    *,
    project: str,
    agent: str,
    agent_state: dict[str, object],
    resume_from: str,
    existing_metadata: dict[str, object] | None,
) -> dict[str, object]:
    started_at = timestamp_utc()
    if existing_metadata is None:
        return {
            "project": project,
            "agent": agent,
            "slug": str(agent_state.get("slug") or ""),
            "status": "running",
            "started_at": started_at,
            "updated_at": started_at,
            "current_phase": resume_from,
            "next_phase": resume_from,
            "completed_phases": [],
            "commit_rounds": 0,
            "repair_loop_path": str(_repair_loop_metadata_path(project, agent)),
            "closeout_prep_logs": [],
            "notes": [],
        }

    payload = dict(existing_metadata)
    payload["status"] = "running"
    payload["resumed_at"] = started_at
    payload["updated_at"] = started_at
    payload["current_phase"] = resume_from
    payload["next_phase"] = resume_from
    if not isinstance(payload.get("completed_phases"), list):
        payload["completed_phases"] = []
    if not isinstance(payload.get("closeout_prep_logs"), list):
        payload["closeout_prep_logs"] = []
    if not isinstance(payload.get("notes"), list):
        payload["notes"] = []
    return payload


def _set_complete_metadata(
    metadata_path: Path,
    payload: dict[str, object],
    *,
    status: str | None = None,
    current_phase: str | None = None,
    next_phase: str | None = None,
    stop_reason: str | None = None,
    note: str | None = None,
) -> None:
    if status is not None:
        payload["status"] = status
    if current_phase is not None:
        payload["current_phase"] = current_phase
    if next_phase is not None:
        payload["next_phase"] = next_phase
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    if note is not None:
        notes = payload.get("notes")
        if not isinstance(notes, list):
            notes = []
        notes.append(note)
        payload["notes"] = notes
    payload["updated_at"] = timestamp_utc()
    _write_json_artifact(metadata_path, payload)


def _cascade_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _running_in_docker_container() -> bool:
    return Path("/.dockerenv").exists()


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].strip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None

    value = value.strip()
    if value and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def load_repo_env_defaults(repo_root: Path | None = None) -> dict[str, str]:
    """Load selected environment defaults from repo `.env` for host-native runs.

    Existing exported environment variables always win.
    """
    if _running_in_docker_container():
        return {}

    root = repo_root or _cascade_repo_root()
    env_file = root / ".env"
    if not env_file.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key not in _AUTOLOAD_ENV_KEYS:
            continue
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded[key] = value

    return loaded


def _load_log_tail(log_path: Path, max_lines: int = 120) -> str:
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _resolve_routing_rule(project_config: ProjectConfig, category: str) -> dict[str, str]:
    configured = project_config.repair_routing.get(category)
    if configured is not None:
        return {
            "strategy": configured.strategy,
            "profile": configured.profile or "debugger",
        }
    configured_unknown = project_config.repair_routing.get("unknown")
    if configured_unknown is not None:
        return {
            "strategy": configured_unknown.strategy,
            "profile": configured_unknown.profile or "debugger",
        }
    return _DEFAULT_REPAIR_ROUTING.get(category, _DEFAULT_REPAIR_ROUTING["unknown"])


def _select_model_profile_for_category(
    category: str,
    options: LoopRunOptions,
    project_config: ProjectConfig,
) -> str:
    if options.profile is not None:
        return options.profile

    rule = _resolve_routing_rule(project_config, category)
    configured_profile = str(rule.get("profile", "debugger"))
    if configured_profile == "cheap_coder":
        return options.cheap_profile
    if configured_profile == "executor":
        return options.executor_profile
    return options.debug_profile


def _forbidden_touched_files(
    touched_after: list[str],
    patterns: list[str],
) -> list[str]:
    forbidden: list[str] = []
    for file_path in touched_after:
        for pattern in patterns:
            if fnmatch.fnmatch(file_path, pattern):
                forbidden.append(file_path)
                break
    return sorted(set(forbidden))


def _build_loop_fix_prompt(
    *,
    category: str,
    hook: str,
    log_tail: str,
    touched_files: list[str],
    diff_stat: str,
) -> str:
    touched = "\n".join(f"- {path}" for path in touched_files[:50]) or "- (none)"
    return (
        "You are fixing one specific deterministic gate failure.\n\n"
        f"Failure category: {category}\n"
        f"Failed hook/check: {hook}\n\n"
        "Relevant log tail:\n"
        f"{log_tail}\n\n"
        "Touched files from gate snapshot:\n"
        f"{touched}\n\n"
        "Current diff stat:\n"
        f"{diff_stat}\n\n"
        "Instructions:\n"
        "- Fix only this specific failure.\n"
        "- No unrelated refactors.\n"
        "- Do not weaken, bypass, disable, or edit gates/enforcement.\n"
        "- Do not stage, commit, or push.\n"
        "- Keep edits inside the assigned worktree and configured project paths.\n"
        "- After fixing, stop and report changed files only.\n"
    )


def print_error(message: str) -> None:
    console.print(f"[red]Error:[/red] {message}")


def print_warning(message: str) -> None:
    console.print(f"[yellow]Warning:[/yellow] {message}")


def load_project_from_agent_state(agent_state: dict[str, object]) -> ProjectConfig | None:
    project_file = _resolve_project_file_from_state(agent_state)
    if project_file is None:
        return None
    try:
        return load_project_config(project_file)
    except ConfigError:
        return None


def _resolve_project_file_from_state(agent_state: dict[str, object]) -> Path | None:
    project_file_value = agent_state.get("project_file")
    if not isinstance(project_file_value, str) or not project_file_value:
        return None

    direct = Path(project_file_value)
    if direct.exists():
        return direct

    if project_file_value.startswith("/workspace/cascade/"):
        remapped = Path.cwd() / project_file_value.removeprefix("/workspace/cascade/")
        if remapped.exists():
            return remapped

    fallback = Path.cwd() / "examples" / "jungle.yaml"
    if fallback.exists():
        return fallback
    return None


def _migrate_agent_state_if_needed(
    *,
    project_name: str,
    agent: str,
    agent_state: dict[str, object],
    project_config: ProjectConfig,
) -> dict[str, object]:
    stale = detect_docker_era_state(agent_state)
    if not stale:
        return agent_state
    migrated, changes = migrate_docker_era_state(agent_state, project_config=project_config)
    if changes:
        save_agent_state(project_name, agent, migrated)
        print_warning("Migrated Docker-era agent state paths for host-native run.")
    return migrated


def require_existing_worktree(agent_state: dict[str, object]) -> Path:
    worktree = Path(str(agent_state["worktree"]))
    if not worktree.exists():
        raise FileNotFoundError(f"Worktree does not exist: {worktree}")
    return worktree


def _opencode_external_directory_warning(project: ProjectConfig) -> str | None:
    """Return a warning string if the project has workspace_links but no opencode.json external_directory config.

    When workspace_links are configured, OpenCode will encounter symlinks inside the worktree that resolve
    to paths outside the worktree cwd, triggering repeated permission prompts.  A project-level opencode.json
    with ``permission.external_directory`` silences those prompts permanently.
    """
    if not project.workspace_links:
        return None

    workspace_root = resolve_workspace_root(project)
    if workspace_root is None:
        return None

    opencode_config_path = workspace_root / "opencode.json"
    if not opencode_config_path.exists():
        return (
            f"Project has workspace_links but no opencode.json found at {opencode_config_path}. "
            "OpenCode will prompt for external_directory permission on every symlinked path. "
            "Add a project-level opencode.json with permission.external_directory allow rules to suppress these prompts."
        )

    try:
        import json as _json

        config = _json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(config, dict):
        return None
    permission = config.get("permission", {})
    if not isinstance(permission, dict) or "external_directory" not in permission:
        return (
            f"opencode.json at {opencode_config_path} has no permission.external_directory section. "
            "OpenCode may still prompt for external paths reached via workspace_links symlinks. "
            "Add permission.external_directory allow rules to suppress these prompts."
        )
    return None


def emit_standards_warnings(project: ProjectConfig | None, agent_state: dict[str, object], worktree: Path) -> None:
    if project is None:
        print_warning("Agent state project_file missing or invalid; skipping standards checks tied to project config.")
        return

    is_valid_location, location_message = validate_worktree_location(project, worktree)
    if not is_valid_location:
        print_warning(location_message)
    branch_warning = validate_agent_branch(project, agent_state, worktree)
    if branch_warning is not None:
        print_warning(branch_warning)
    for warning in validate_instruction_files(project):
        print_warning(warning)
    opencode_warning = _opencode_external_directory_warning(project)
    if opencode_warning is not None:
        print_warning(opencode_warning)


def resolve_prompt_path(run_dir: Path, task: str | None = None, prompt_file: Path | None = None) -> Path:
    if not isinstance(run_dir, Path):
        raise TypeError(f"run_dir must be pathlib.Path, got {type(run_dir).__name__}: {run_dir!r}")

    if prompt_file is not None:
        if not isinstance(prompt_file, Path):
            raise TypeError(f"prompt_file must be pathlib.Path, got {type(prompt_file).__name__}: {prompt_file!r}")
        path = prompt_file
    elif task is not None:
        path = run_dir / f"{task}_prompt.md"
    else:
        path = run_dir / "launch_prompt.md"

    if not isinstance(path, Path):
        raise TypeError(f"Resolved prompt path must be pathlib.Path, got {type(path).__name__}: {path!r}")

    exists_attr = getattr(path, "exists", None)
    if not callable(exists_attr):
        raise TypeError(
            f"Resolved prompt path has invalid exists attribute for {path!r}: "
            f"{type(exists_attr).__name__}={exists_attr!r}"
        )

    if not exists_attr():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path


def build_prompt_copy_command(
    agent: str,
    project: str,
    task: str | None = None,
    prompt_file: Path | None = None,
) -> str:
    if prompt_file is not None:
        return f"cat {shlex.quote(str(prompt_file))} | pbcopy"

    command_parts = ["cascade", "show-prompt", agent, "--project", project]
    if task is not None:
        command_parts.extend(["--task", task])
    return f"{' '.join(shlex.quote(part) for part in command_parts)} | pbcopy"


def resolve_mandate_start_template(project: ProjectConfig) -> str | None:
    return (
        project.commands.mandate_start
        or project.commands.start_mandate
        or project.commands.init_mandate
    )


def format_command_template(
    template: str,
    *,
    project: ProjectConfig,
    agent: str,
    slug: str,
    issue: int,
    title: str,
    active_branch: str = "",
    canonical_mandate: Path | None = None,
    branch: str = "",
) -> str:
    canonical = str(canonical_mandate) if canonical_mandate is not None else ""
    return template.format(
        agent=agent,
        slug=slug,
        branch=branch,
        issue=issue,
        project=project.name,
        title=title,
        title_shell=shlex.quote(title),
        active_branch=active_branch,
        active_branch_shell=shlex.quote(active_branch),
        canonical_mandate=canonical,
        canonical_mandate_shell=shlex.quote(canonical),
    )


def resolve_active_branch(
    project: ProjectConfig,
    *,
    active_branch_override: str | None = None,
) -> str | None:
    if active_branch_override is not None and active_branch_override.strip():
        return active_branch_override.strip()
    for candidate in (
        project.branches.active_branch,
        project.branches.base,
        project.default_active_branch,
    ):
        if candidate is not None and candidate.strip():
            return candidate.strip()
    return None


def worktree_is_agent_branch(
    project: ProjectConfig,
    *,
    worktree: Path,
    agent: str,
    slug: str,
) -> bool:
    current_branch = get_current_branch(worktree)
    expected = build_branch_name(project, agent, slug)
    return current_branch == expected


def get_agent_branch_mismatch_error(
    project: ProjectConfig,
    *,
    worktree: Path,
    agent: str,
    slug: str,
) -> str | None:
    current_branch = get_current_branch(worktree)
    if current_branch.startswith("(unable") or current_branch == "(detached HEAD)":
        return None
    expected = build_branch_name(project, agent, slug)
    if current_branch == expected:
        return None
    return f"Branch mismatch: expected '{expected}', found '{current_branch}'."


def format_mandate_start_command(
    project: ProjectConfig,
    *,
    agent: str,
    slug: str,
    issue: int,
    title: str,
    active_branch: str,
    canonical_mandate: Path,
) -> str | None:
    template = resolve_mandate_start_template(project)
    if template is None:
        return None
    return format_command_template(
        template,
        project=project,
        agent=agent,
        slug=slug,
        issue=issue,
        title=title,
        active_branch=active_branch,
        canonical_mandate=canonical_mandate,
    )


def mandate_metadata_dir(worktree: Path) -> Path:
    return worktree / ".github" / "mandates"


def mandate_metadata_path(worktree: Path, slug: str) -> Path:
    return mandate_metadata_dir(worktree) / f"{slug}.json"


def _normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                normalized.append(stripped)
    return normalized


def _infer_mandate_id(
    *,
    worktree: Path,
    existing_payload: dict[str, object],
) -> str:
    existing = existing_payload.get("mandate_id")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()

    project_meta = read_github_project_config(worktree)
    prefix_raw = ""
    if isinstance(project_meta, dict):
        prefix_raw = str(project_meta.get("repo_prefix") or "").strip().upper()
    prefix = prefix_raw or "MAND"

    audit_path = worktree / ".github" / "mandates" / "audit.log"
    if audit_path.exists():
        pattern = re.compile(rf"\b{re.escape(prefix)}-\d{{8}}-\d{{3}}\b")
        for line in reversed(audit_path.read_text(encoding="utf-8").splitlines()):
            match = pattern.search(line)
            if match is not None:
                return match.group(0)

    return f"{prefix}-{time.strftime('%m%d%Y')}-000"


def _canonical_mandate_metadata_payload(
    project_config: ProjectConfig,
    *,
    worktree: Path,
    slug: str,
    agent: str,
    active_branch: str,
    canonical_mandate: Path,
    agent_state: dict[str, object],
    existing_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    payload_source = existing_payload or {}

    title_value = str(payload_source.get("title") or "").strip()
    if not title_value:
        title_value = str(agent_state.get("title") or "").strip()

    github_item_id = str(payload_source.get("github_project_item_id") or "").strip()
    if not github_item_id:
        github_item_id = str(agent_state.get("github_project_item_id") or "").strip()

    file_scope = _normalize_string_list(payload_source.get("file_scope"))
    if not file_scope:
        file_scope = [
            ".github/mandates/audit.log",
            f".github/mandates/{slug}.json",
        ]
        try:
            canonical_relative = canonical_mandate.resolve().relative_to(worktree.resolve())
            file_scope.append(str(canonical_relative))
        except ValueError:
            pass

    commits = _normalize_string_list(payload_source.get("commits"))

    precommit_failures_raw = payload_source.get("precommit_failures", 0)
    try:
        precommit_failures = int(precommit_failures_raw or 0)
    except (TypeError, ValueError):
        precommit_failures = 0
    if precommit_failures < 0:
        precommit_failures = 0

    canonical_payload: dict[str, object] = {
        "slug": slug,
        "mandate_id": _infer_mandate_id(worktree=worktree, existing_payload=payload_source),
        "status": "in_progress",
        "github_project_item_id": github_item_id,
        "agent_branch": build_branch_name(project_config, agent, slug),
        "active_branch": active_branch,
        "repo": project_config.github.repo,
        "canonical_mandate": str(canonical_mandate.resolve()),
        "worktree_path": str(worktree.resolve()),
        "file_scope": file_scope,
        "commits": commits,
        "precommit_failures": precommit_failures,
    }

    if title_value:
        canonical_payload["title"] = title_value

    created_at_value = payload_source.get("created_at")
    if isinstance(created_at_value, str) and created_at_value.strip():
        canonical_payload["created_at"] = created_at_value.strip()
    else:
        canonical_payload["created_at"] = timestamp_utc()

    canonical_payload["updated_at"] = timestamp_utc()
    return canonical_payload


def _ensure_mandate_metadata_files_exist(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    active_branch_override: str | None = None,
    dry_run: bool,
) -> tuple[bool, str, Path | None]:
    worktree = Path(str(agent_state.get("worktree", "")))
    slug = str(agent_state.get("slug", "")).strip()
    agent = str(agent_state.get("agent", "")).strip()
    if not worktree.exists() or not slug or not agent:
        return False, "No worktree/slug/agent available for mandate metadata creation.", None

    active_branch = resolve_active_branch(project_config, active_branch_override=active_branch_override)
    if active_branch is None:
        return False, "Active branch is required for mandate metadata creation.", None

    run_dir_for_agent = get_agent_run_dir(project_config.name, agent)
    canonical_mandate = run_dir_for_agent / "mandate.md"
    metadata = mandate_metadata_path(worktree, slug)
    metadata_dir = mandate_metadata_dir(worktree)
    audit_log = metadata_dir / "audit.log"

    payload_obj: dict[str, object] = {}
    if metadata.exists():
        try:
            parsed = json.loads(metadata.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                payload_obj = parsed
        except json.JSONDecodeError:
            payload_obj = {}

    payload = _canonical_mandate_metadata_payload(
        project_config,
        worktree=worktree,
        slug=slug,
        agent=agent,
        active_branch=active_branch,
        canonical_mandate=canonical_mandate,
        agent_state=agent_state,
        existing_payload=payload_obj,
    )

    if dry_run:
        return True, f"Dry run: would ensure mandate metadata files at {metadata} and {audit_log}", metadata

    metadata_dir.mkdir(parents=True, exist_ok=True)
    if not metadata.exists():
        metadata.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    else:
        metadata.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if not audit_log.exists():
        audit_log.write_text("", encoding="utf-8")

    return True, f"Mandate metadata files ensured at {metadata}.", metadata


def _expected_mandate_metadata_fields(
    project_config: ProjectConfig,
    *,
    worktree: Path,
    slug: str,
    agent: str,
    active_branch: str,
    canonical_mandate: Path,
) -> dict[str, str]:
    payload = _canonical_mandate_metadata_payload(
        project_config,
        worktree=worktree,
        slug=slug,
        agent=agent,
        active_branch=active_branch,
        canonical_mandate=canonical_mandate,
        agent_state={
            "agent": agent,
            "slug": slug,
            "worktree": str(worktree),
        },
        existing_payload={},
    )
    return {
        key: value
        for key, value in payload.items()
        if isinstance(value, str)
    }


def _detect_mandate_metadata_field_drift(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    active_branch_override: str | None = None,
) -> list[str]:
    worktree = Path(str(agent_state.get("worktree", "")))
    slug = str(agent_state.get("slug", "")).strip()
    agent = str(agent_state.get("agent", "")).strip()
    if not worktree.exists() or not slug or not agent:
        return []

    metadata = mandate_metadata_path(worktree, slug)
    if not metadata.exists():
        return []

    active_branch = resolve_active_branch(project_config, active_branch_override=active_branch_override)
    if active_branch is None:
        return ["Active branch is required for mandate metadata repair."]

    run_dir = get_agent_run_dir(project_config.name, agent)
    canonical_mandate = run_dir / "mandate.md"
    expected = _expected_mandate_metadata_fields(
        project_config,
        worktree=worktree,
        slug=slug,
        agent=agent,
        active_branch=active_branch,
        canonical_mandate=canonical_mandate,
    )

    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["Mandate metadata JSON is invalid."]

    if not isinstance(payload, dict):
        return ["Mandate metadata JSON must be an object."]

    drift: list[str] = []
    for key, expected_value in expected.items():
        actual_value = payload.get(key)
        if not isinstance(actual_value, str) or actual_value != expected_value:
            drift.append(f"{key}: expected '{expected_value}', found '{actual_value}'")
    return drift


def repair_mandate_metadata_fields(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    dry_run: bool = False,
    active_branch_override: str | None = None,
) -> RepairResult:
    agent = str(agent_state.get("agent", "")).strip()
    run_dir = get_agent_run_dir(project_config.name, agent)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "repair_mandate_metadata_fields.log"
    log_lines = [
        "# Repair Mandate Metadata Fields",
        f"timestamp: {timestamp_utc()}",
    ]

    worktree = Path(str(agent_state.get("worktree", "")))
    slug = str(agent_state.get("slug", "")).strip()
    if not worktree.exists() or not slug:
        message = "No worktree/slug available for mandate metadata field repair."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.mandate_metadata,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    metadata = mandate_metadata_path(worktree, slug)
    if not metadata.exists():
        message = f"Mandate metadata file is missing: {metadata}"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.mandate_metadata,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    active_branch = resolve_active_branch(project_config, active_branch_override=active_branch_override)
    if active_branch is None:
        message = "Active branch is required for mandate metadata field repair."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.mandate_metadata,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    run_dir_for_agent = get_agent_run_dir(project_config.name, agent)
    canonical_mandate = run_dir_for_agent / "mandate.md"
    expected = _expected_mandate_metadata_fields(
        project_config,
        worktree=worktree,
        slug=slug,
        agent=agent,
        active_branch=active_branch,
        canonical_mandate=canonical_mandate,
    )

    try:
        payload_obj = json.loads(metadata.read_text(encoding="utf-8"))
        payload_raw = payload_obj if isinstance(payload_obj, dict) else {}
    except json.JSONDecodeError:
        payload_raw = {}

    payload = _canonical_mandate_metadata_payload(
        project_config,
        worktree=worktree,
        slug=slug,
        agent=agent,
        active_branch=active_branch,
        canonical_mandate=canonical_mandate,
        agent_state=agent_state,
        existing_payload=payload_raw,
    )

    updates: list[str] = []
    for key, expected_value in payload.items():
        actual_value = payload_raw.get(key)
        if actual_value != expected_value:
            updates.append(f"{key}: {actual_value!r} -> {expected_value!r}")

    if not updates:
        message = "Mandate metadata fields are already canonical."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.mandate_metadata,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    log_lines.extend(updates)

    if not dry_run:
        metadata.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        staged_ok, staged_message = _stage_and_verify_mandate_metadata_files(
            worktree,
            slug,
            dry_run=False,
        )
        log_lines.append(staged_message)
        if not staged_ok:
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.mandate_metadata,
                success=False,
                dry_run=dry_run,
                message=staged_message,
                log_path=log_path,
            )

    message = "Mandate metadata fields repaired successfully."
    log_lines.append(message)
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return RepairResult(
        kind=RepairKind.mandate_metadata,
        success=True,
        dry_run=dry_run,
        message=message,
        log_path=log_path,
    )


def repo_expects_mandate_metadata(worktree: Path) -> bool:
    return mandate_metadata_dir(worktree).exists()


def detect_missing_mandate_metadata(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    active_branch_override: str | None = None,
) -> RepairFinding | None:
    agent_value = str(agent_state.get("agent", ""))
    if not agent_value:
        return None

    worktree = Path(str(agent_state.get("worktree", "")))
    if not worktree.exists():
        return None
    if not repo_expects_mandate_metadata(worktree):
        return None

    slug = str(agent_state.get("slug", "")).strip()
    title = str(agent_state.get("title", "")).strip()
    if not slug:
        return None

    active_branch = resolve_active_branch(
        project_config,
        active_branch_override=active_branch_override,
    )

    metadata = mandate_metadata_path(worktree, slug)
    if metadata.exists():
        return None

    run_dir = get_agent_run_dir(project_config.name, agent_value)
    canonical_mandate = run_dir / "mandate.md"
    start_command = format_mandate_start_command(
        project_config,
        agent=agent_value,
        slug=slug,
        issue=int(agent_state.get("issue", 0) or 0),
        title=title,
        active_branch=active_branch or "",
        canonical_mandate=canonical_mandate,
    )

    if start_command is not None and active_branch is None and worktree_is_agent_branch(
        project_config,
        worktree=worktree,
        agent=agent_value,
        slug=slug,
    ):
        return RepairFinding(
            kind=RepairKind.missing_mandate_metadata,
            slug=slug,
            title=title,
            worktree=worktree,
            metadata_path=metadata,
            canonical_mandate_path=canonical_mandate,
            message=(
                "Active branch is required for mandate_start. "
                "Configure branches.active_branch or pass --active-branch."
            ),
            can_repair=False,
            repair_command=None,
        )

    if start_command is None:
        return RepairFinding(
            kind=RepairKind.missing_mandate_metadata,
            slug=slug,
            title=title,
            worktree=worktree,
            metadata_path=metadata,
            canonical_mandate_path=canonical_mandate,
            message=(
                "Required mandate metadata is missing: "
                f"{metadata}. This repo expects .github/mandates/<slug>.json. "
                f"{NO_INIT_MANDATE_MESSAGE}"
            ),
            can_repair=False,
            repair_command=None,
        )

    if not title:
        return RepairFinding(
            kind=RepairKind.missing_mandate_metadata,
            slug=slug,
            title=title,
            worktree=worktree,
            metadata_path=metadata,
            canonical_mandate_path=canonical_mandate,
            message=f"Required mandate metadata is missing: {metadata}. Agent title is missing; cannot repair safely.",
            can_repair=False,
            repair_command=None,
        )

    if not canonical_mandate.exists():
        return RepairFinding(
            kind=RepairKind.missing_mandate_metadata,
            slug=slug,
            title=title,
            worktree=worktree,
            metadata_path=metadata,
            canonical_mandate_path=canonical_mandate,
            message=(
                f"Required mandate metadata is missing: {metadata}. "
                f"Canonical mandate file is missing: {canonical_mandate}."
            ),
            can_repair=False,
            repair_command=None,
        )

    return RepairFinding(
        kind=RepairKind.missing_mandate_metadata,
        slug=slug,
        title=title,
        worktree=worktree,
        metadata_path=metadata,
        canonical_mandate_path=canonical_mandate,
        message=f"Required mandate metadata is missing: {metadata}.",
        can_repair=True,
        repair_command=start_command,
    )


def run_git_stash_if_dirty(worktree: Path) -> tuple[str | None, str | None]:
    status = run_command("git status --porcelain", cwd=worktree).stdout.strip()
    if not status:
        return None, None

    stash_message = f"cascade-repair-before-mandate-start-{timestamp_utc()}"
    run_command(f"git stash push -u -m {shlex.quote(stash_message)}", cwd=worktree)
    stash_ref = run_command("git stash list --format=%gd -n 1", cwd=worktree).stdout.strip()
    return stash_ref or None, stash_message


def restore_git_stash(worktree: Path, stash_ref: str | None) -> tuple[bool, str | None]:
    if stash_ref is None:
        return True, None
    try:
        run_command(f"git stash pop {shlex.quote(stash_ref)}", cwd=worktree)
    except CommandError as exc:
        return False, str(exc)
    return True, None


def _mandate_metadata_stage_paths(worktree: Path, slug: str) -> list[str]:
    metadata_rel = f".github/mandates/{slug}.json"
    audit_rel = ".github/mandates/audit.log"
    stage_paths = [metadata_rel]
    if (worktree / audit_rel).exists():
        stage_paths.append(audit_rel)
    return stage_paths


def _stage_and_verify_mandate_metadata_files(
    worktree: Path,
    slug: str,
    *,
    dry_run: bool,
) -> tuple[bool, str]:
    metadata_path = worktree / ".github" / "mandates" / f"{slug}.json"
    if not metadata_path.exists():
        return False, f"Mandate metadata file is missing after repair: {metadata_path}"

    stage_paths = _mandate_metadata_stage_paths(worktree, slug)
    quoted_paths = " ".join(shlex.quote(path) for path in stage_paths)

    if dry_run:
        return True, f"Dry run: would stage mandate metadata files: {', '.join(stage_paths)}"

    try:
        run_command(f"git add -- {quoted_paths}", cwd=worktree)
    except CommandError as exc:
        return False, f"Failed to stage repaired mandate metadata files: {exc}"

    for rel_path in stage_paths:
        try:
            run_command(f"git ls-files --error-unmatch -- {shlex.quote(rel_path)}", cwd=worktree)
        except CommandError as exc:
            return False, (
                "Mandate metadata repair left files unsuitable for preflight stash/rebase flow; "
                f"tracking check failed for {rel_path}: {exc}"
            )

    try:
        status_output = run_command(f"git status --porcelain -- {quoted_paths}", cwd=worktree).stdout
    except CommandError as exc:
        return False, f"Failed to verify git status for repaired mandate metadata files: {exc}"

    for line in status_output.splitlines():
        if not line.strip() or len(line) < 3:
            continue
        status_code = line[:2]
        file_fragment = line[3:].strip()
        if status_code == "??":
            return False, (
                "Mandate metadata repair left untracked files that break preflight stash/rebase flow: "
                f"{file_fragment}"
            )
        if status_code[0] == " " and status_code[1] != " ":
            return False, (
                "Mandate metadata repair did not fully stage repaired files; "
                f"unstaged changes remain in {file_fragment}"
            )

    return True, "Mandate metadata files staged and verified for preflight stash/rebase flow."


def _parse_git_status_porcelain(output: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip() or len(raw_line) < 4:
            continue
        status_code = raw_line[:2]
        path_fragment = raw_line[3:].strip()
        if " -> " in path_fragment:
            path_fragment = path_fragment.split(" -> ", 1)[1].strip()
        entries.append((status_code, path_fragment))
    return entries


def _is_obvious_scratch_file(path: str) -> bool:
    """Check if a file path looks like obvious scratch/debug/temporary content.
    
    Returns True if the file matches patterns of:
    - Debug files: test_debug.py, debug*.py, *_test_*.py
    - Temp files: tmp*, *.bak, *.tmp, *.swp, *.swo, ~*
    - System files: .DS_Store, *.pyc, __pycache__
    - Hidden scratch files: .cache, .coverage*, etc.
    """
    basename = Path(path).name
    path_lower = path.lower()
    
    # Debug/test scratch patterns
    debug_patterns = [
        "test_debug.py",
        "debug_",
        "_debug.py",
        "_test_",
        "scratch",
        "temp_",
    ]
    
    # Temporary/backup patterns
    temp_patterns = [
        ".bak",
        ".tmp",
        ".swp",
        ".swo",
        "~",
        ".DS_Store",
        "__pycache__",
    ]
    
    for pattern in debug_patterns:
        if pattern in path_lower:
            return True
    
    for pattern in temp_patterns:
        if pattern in path_lower:
            return True
    
    # Python compiled files
    if path_lower.endswith(".pyc"):
        return True
    
    # Coverage files  
    if ".coverage" in path_lower:
        return True
    
    # Hidden cache/build artifacts
    if basename.startswith(".") and any(x in path_lower for x in ["cache", "temp", "build"]):
        return True
    
    return False


def _is_freshly_implemented_mandate(worktree: Path, mandate_payload: dict[str, object] | None) -> bool:
    """Check if mandate appears to be freshly implemented (HEAD == active_branch commit).
    
    For freshly implemented mandates, implementation files are dirty/added but not yet
    committed, so git diff won't detect them. This function identifies such mandates
    so that additional heuristics can be applied.
    """
    if mandate_payload is None:
        return False
    
    active_branch = mandate_payload.get("active_branch")
    if not isinstance(active_branch, str) or not active_branch.strip():
        return False
    
    active_branch = active_branch.strip()
    
    try:
        # Check if HEAD and active_branch point to the same commit
        branch_ref = active_branch
        
        # Try to use origin branch first
        try:
            run_command(f"git rev-parse origin/{branch_ref}", cwd=worktree)
            branch_ref = f"origin/{branch_ref}"
        except CommandError:
            # Fall back to local branch reference - no need to catch Exception here
            pass
        except Exception:
            # If something unexpected happens, still try with local branch
            pass
        
        # Get commit hashes - these should not raise CommandError if branch exists
        head_commit = run_command(
            "git rev-parse HEAD",
            cwd=worktree,
        ).stdout.strip()
        
        branch_commit = run_command(
            f"git rev-parse {branch_ref}",
            cwd=worktree,
        ).stdout.strip()
        
        # If HEAD == branch tip, mandate is freshly implemented
        return head_commit == branch_commit
    except CommandError:
        # Git command failed
        return False
    except Exception:
        # Unexpected error
        return False


def _get_impl_directory_files_for_fresh_mandate(dirty_paths: list[str]) -> set[str]:
    """For freshly implemented mandates, extract files in implementation directories.
    
    Returns files from dirty_paths that are in common implementation directories,
    allowing early identification of mandate-owned files before they're committed.
    """
    impl_dirs = ["jungle/", "web/", "src/", "backend/", "frontend/", "app/", "api/", "inventory/"]
    return {path for path in dirty_paths if any(path.startswith(dir) for dir in impl_dirs)}


def _get_files_changed_on_branch(
    worktree: Path,
    mandate_payload: dict[str, object] | None,
) -> set[str]:
    """Get files that have been modified on the mandate branch relative to active_branch.
    
    Returns a set of file paths that exist in a diff from the active branch to HEAD.
    If active_branch is not available, returns an empty set.
    """
    if mandate_payload is None:
        return set()
    
    active_branch = mandate_payload.get("active_branch")
    if not isinstance(active_branch, str) or not active_branch.strip():
        return set()
    
    active_branch = active_branch.strip()
    
    try:
        # Get files changed between active_branch and HEAD
        # Use origin/branch if available, fall back to local branch
        branch_ref = active_branch
        try:
            # Try origin/branch first for most robust results
            run_command(f"git rev-parse origin/{branch_ref}", cwd=worktree)
            branch_ref = f"origin/{branch_ref}"
        except CommandError:
            # Fall back to local branch name
            pass
        
        # Get diff from branch to HEAD
        output = run_command(
            f"git diff --name-only {branch_ref}...HEAD",
            cwd=worktree,
        ).stdout.strip()
        
        return set(line.strip() for line in output.splitlines() if line.strip())
    except (CommandError, Exception):
        # If branch comparison fails, return empty set
        return set()


def _heuristic_mandate_related_path(
    file_path: str,
    mandate_owned_paths: list[str],
    mandate_scope_patterns: list[str],
    impl_dir_files: set[str] | None = None,
) -> bool:
    """Check if a file is heuristically related to mandate-owned files.
    
    Returns True if:
    - File is a test file (test_*.py, *_test.py) in same directory as mandate files
    - File is in the same directory as a mandate-owned file
    - File is in common implementation directories for freshly implemented mandates
    - File matches a pattern like jungle/*, web/*, etc. when mandate has impl files
    """
    file_lower = file_path.lower()
    file_obj = Path(file_path)
    
    # Pattern 1: test files in same directory as mandate files
    if ("test_" in file_lower or file_lower.endswith("_test.py")):
        # Check if same directory (not just parent) has mandate files
        file_dir = str(file_obj.parent)
        for mandate_path in mandate_owned_paths:
            mandate_dir = str(Path(mandate_path).parent)
            # Same directory only (not sibling)
            if file_dir == mandate_dir:
                return True
    
    # Pattern 2: file in same directory as mandate-owned file
    for mandate_path in mandate_owned_paths:
        if str(file_obj.parent) == str(Path(mandate_path).parent):
            return True
    
    # Pattern 3: common implementation directories
    impl_dirs = ["jungle/", "web/", "src/", "backend/", "frontend/", "app/", "api/", "inventory/"]
    if any(file_path.startswith(dir) for dir in impl_dirs):
        # For freshly implemented mandates, include all impl-dir files
        if impl_dir_files and file_path in impl_dir_files:
            return True
        # Otherwise check if any mandate pattern is in impl dirs
        for pattern in mandate_scope_patterns:
            if any(pattern.startswith(dir) for dir in impl_dirs):
                return True
    
    return False


def _classify_mandate_files_with_signals(
    *,
    worktree: Path,
    slug: str,
    dirty_paths: list[str],
    mandate_payload: dict[str, object] | None,
    scope_patterns: list[str],
) -> tuple[list[str], list[str]]:
    """Classify dirty files into mandate-owned and suspicious using multiple signals.
    
    Uses four signals to classify mandate-owned files:
    1. Metadata scope patterns (declared file_scope)
    2. Files changed on mandate branch (git diff from active_branch to HEAD)
    3. Heuristic relationships (test files in same dir, etc.)
    4. Implementation directory files for freshly implemented mandates
    
    For freshly implemented mandates (HEAD == active_branch commit), also includes
    dirty files in implementation directories since they haven't been committed yet.
    
    Files are classified as suspicious if they:
    - Don't match any of the above signals, AND
    - Are obvious scratch/debug files
    
    Files that don't match signals AND aren't obvious scratch files are treated as
    suspicious to be conservative (user must explicitly include them if intended).
    
    Returns: (mandate_owned_paths, suspicious_paths)
    """
    mandate_owned_paths: list[str] = []
    suspicious_paths: list[str] = []
    
    # Get signals for classification
    branch_changed_files = _get_files_changed_on_branch(worktree, mandate_payload)
    is_fresh_mandate = _is_freshly_implemented_mandate(worktree, mandate_payload)
    impl_dir_files = _get_impl_directory_files_for_fresh_mandate(dirty_paths) if is_fresh_mandate else None
    
    # For fresh mandates, identify implementation directories at git status level
    # (untracked files appear as directory entries like "api/" or "jungle/")
    impl_dirs = ["jungle/", "web/", "src/", "backend/", "frontend/", "app/", "api/", "inventory/"]
    impl_dir_entries = {path for path in dirty_paths if path in impl_dirs and is_fresh_mandate}
    
    for dirty_path in dirty_paths:
        # Skip obvious scratch files entirely - they shouldn't be considered for any signal
        if _is_obvious_scratch_file(dirty_path):
            suspicious_paths.append(dirty_path)
            continue
        
        # Signal 1: Check metadata scope patterns
        matches_scope = any(fnmatch.fnmatch(dirty_path, pattern) for pattern in scope_patterns)
        
        # Signal 2: Check if file was changed on mandate branch
        matches_branch = dirty_path in branch_changed_files
        
        # Signal 3: Check heuristic relationships
        matches_heuristic = _heuristic_mandate_related_path(
            dirty_path,
            mandate_owned_paths,
            scope_patterns,
            impl_dir_files,
        )
        
        # Signal 4a: For freshly implemented mandates, impl-dir files are mandate-owned
        matches_fresh_impl = is_fresh_mandate and impl_dir_files and dirty_path in impl_dir_files
        
        # Signal 4b: For freshly implemented mandates, impl-dir entries (directory-level)
        # from untracked files are mandate-owned
        matches_fresh_impl_dir = dirty_path in impl_dir_entries
        
        # Classify based on signals
        if matches_scope or matches_branch or matches_heuristic or matches_fresh_impl or matches_fresh_impl_dir:
            mandate_owned_paths.append(dirty_path)
        else:
            # File doesn't match any signal - classify as suspicious
            # (could be unrelated modification)
            suspicious_paths.append(dirty_path)
    
    return mandate_owned_paths, suspicious_paths


def _normalize_mandate_scope_patterns(
    *,
    worktree: Path,
    slug: str,
    mandate_payload: dict[str, object] | None,
    run_dir: Path,
) -> list[str]:
    patterns: list[str] = [
        f".github/mandates/{slug}.json",
        ".github/mandates/audit.log",
    ]

    if mandate_payload is not None:
        file_scope = mandate_payload.get("file_scope")
        if isinstance(file_scope, list):
            for raw_item in file_scope:
                if isinstance(raw_item, str) and raw_item.strip():
                    patterns.append(raw_item.strip())

        canonical_mandate_raw = mandate_payload.get("canonical_mandate")
        if isinstance(canonical_mandate_raw, str) and canonical_mandate_raw.strip():
            try:
                canonical_relative = str(Path(canonical_mandate_raw).resolve().relative_to(worktree.resolve()))
                patterns.append(canonical_relative)
            except ValueError:
                pass

    canonical_in_run_dir = run_dir / "mandate.md"
    try:
        canonical_relative = str(canonical_in_run_dir.resolve().relative_to(worktree.resolve()))
        patterns.append(canonical_relative)
    except ValueError:
        pass

    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in patterns:
        if pattern not in seen:
            ordered.append(pattern)
            seen.add(pattern)
    return ordered


def _build_safe_commit_message(mandate_id: str, raw_message: str | None, slug: str) -> str:
    if raw_message is None or not raw_message.strip():
        return f"{mandate_id} {slug} implementation checkpoint"
    stripped = raw_message.strip()
    if stripped.startswith(mandate_id):
        return stripped
    return f"{mandate_id} {stripped}"


def prepare_mandate_closeout_dirty_files(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    stage: bool,
    commit: bool,
    yes: bool,
    dry_run: bool,
    commit_message: str | None,
) -> RepairResult:
    agent_name = str(agent_state.get("agent", "unknown"))
    slug = str(agent_state.get("slug", "")).strip()
    run_dir = get_agent_run_dir(project_config.name, agent_name)
    log_path = run_dir / "closeout_prep.log"
    log_lines = [
        "# Mandate Closeout Prep",
        f"timestamp: {timestamp_utc()}",
        f"agent: {agent_name}",
        f"slug: {slug}",
        f"stage: {stage}",
        f"commit: {commit}",
        f"dry_run: {dry_run}",
    ]

    worktree_str = str(agent_state.get("worktree", ""))
    if not worktree_str:
        message = "Agent state missing worktree path."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    worktree = Path(worktree_str)
    if not worktree.exists():
        message = f"Worktree does not exist: {worktree}"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    status_output = run_command("git status --porcelain", cwd=worktree).stdout
    entries = _parse_git_status_porcelain(status_output)
    dirty_paths = [path for _status, path in entries]
    log_lines.append(f"dirty_paths: {dirty_paths}")

    mandate_payload = read_mandate_metadata(worktree, slug)
    scope_patterns = _normalize_mandate_scope_patterns(
        worktree=worktree,
        slug=slug,
        mandate_payload=mandate_payload,
        run_dir=run_dir,
    )
    metadata_paths = _mandate_metadata_stage_paths(worktree, slug)
    log_lines.append(f"scope_patterns: {scope_patterns}")

    # Use multi-signal classification to determine mandate-owned vs suspicious files
    mandate_owned_paths, suspicious_paths = _classify_mandate_files_with_signals(
        worktree=worktree,
        slug=slug,
        dirty_paths=dirty_paths,
        mandate_payload=mandate_payload,
        scope_patterns=scope_patterns,
    )
    log_lines.append(f"mandate_owned_paths: {mandate_owned_paths}")
    log_lines.append(f"suspicious_paths: {suspicious_paths}")

    if not dirty_paths:
        message = "No dirty files found. Working tree is clean."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    if not mandate_owned_paths:
        message = (
            "Dirty files are outside mandate scope. No files staged. "
            f"Suspicious files: {', '.join(suspicious_paths)}"
        )
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    stage_candidates: list[str] = []
    for path in mandate_owned_paths + metadata_paths:
        if path not in stage_candidates:
            stage_candidates.append(path)
    log_lines.append(f"stage_candidates: {stage_candidates}")

    if stage and not dry_run:
        quoted_paths = " ".join(shlex.quote(path) for path in stage_candidates)
        run_command(f"git add -- {quoted_paths}", cwd=worktree)
        log_lines.append("staged_mandate_paths: true")
    elif stage and dry_run:
        log_lines.append("staged_mandate_paths: dry_run")

    mandate_id = read_mandate_id(worktree, slug)
    prepared_message: str | None = None
    if commit:
        if not yes:
            message = "Commit requested but --yes was not provided."
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )
        if suspicious_paths:
            message = (
                "Commit blocked: suspicious dirty files are present outside mandate scope: "
                f"{', '.join(suspicious_paths)}"
            )
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )
        if not mandate_id:
            message = "Commit blocked: mandate_id missing from mandate metadata."
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        prepared_message = _build_safe_commit_message(mandate_id, commit_message, slug)
        log_lines.append(f"commit_message: {prepared_message}")
        if dry_run:
            message = (
                "Dry run: would stage mandate-owned dirty files and create commit: "
                f"{prepared_message}"
            )
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=True,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        if not stage:
            quoted_paths = " ".join(shlex.quote(path) for path in stage_candidates)
            run_command(f"git add -- {quoted_paths}", cwd=worktree)
            log_lines.append("staged_mandate_paths: true")

        staged_names = run_command("git diff --cached --name-only", cwd=worktree).stdout.splitlines()
        staged_set = {name.strip() for name in staged_names if name.strip()}
        allowed_set = set(stage_candidates)
        unexpected_staged = sorted(path for path in staged_set if path not in allowed_set)
        if unexpected_staged:
            message = (
                "Commit blocked: staged set includes paths outside mandate-prep selection: "
                f"{', '.join(unexpected_staged)}"
            )
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        commit_command = f"git commit -m {shlex.quote(prepared_message)}"
        try:
            run_command(commit_command, cwd=worktree)
        except CommandError as exc:
            commit_failure_log_path = run_dir / "closeout_prep_commit_failure.log"
            commit_failure_log_path.write_text(str(exc.output), encoding="utf-8")

            try:
                staged_names_output = run_command("git diff --cached --name-only", cwd=worktree).stdout
                touched_files = [line.strip() for line in staged_names_output.splitlines() if line.strip()]
            except CommandError:
                touched_files = []
            classification = classify_gate_failure(str(exc.output))
            failure_context = {
                "source": "closeout-prep-commit",
                "timestamp": timestamp_utc(),
                "command": commit_command,
                "hook": str(classification.get("hook") or "unknown"),
                "log": str(exc.output),
                "log_path": str(commit_failure_log_path),
                "touched_files": touched_files,
            }
            _save_failure_context(run_dir, _COMMIT_FAILURE_CONTEXT_FILENAME, failure_context)
            _save_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME, failure_context)

            message = "Commit failed during closeout-prep. Review gate output or run gate-fix."
            log_lines.append(message)
            log_lines.append("commit_failure_log: closeout_prep_commit_failure.log")
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.closeout_dirty_file_prep,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        _clear_failure_context(run_dir, _COMMIT_FAILURE_CONTEXT_FILENAME)
        _clear_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME)
        message = (
            "Mandate-owned files staged and committed. "
            f"Commit message: {prepared_message}"
        )
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.closeout_dirty_file_prep,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    if suspicious_paths:
        blocked_extras = ", ".join(f"[{path}]" for path in suspicious_paths)
        if stage:
            staged_files = ", ".join(f"[{path}]" for path in mandate_owned_paths)
            message = (
                f"✓ Staged mandate-owned files: {staged_files}. "
                f"⚠ Blocked suspicious extras (unrelated to mandate): {blocked_extras}. "
                f"Clean or move suspicious files and re-run with --stage to proceed with commit."
            )
        else:
            message = (
                f"Mandate-owned files ready to stage: {', '.join(mandate_owned_paths)}. "
                f"⚠ Suspicious extras detected (unrelated to mandate): {blocked_extras}. "
                f"Use --stage to stage mandate files, then manually clean suspicious extras and commit."
            )
    else:
        if stage:
            staged_files = ", ".join(f"[{path}]" for path in mandate_owned_paths)
            message = (
                f"✓ Mandate-owned dirty files and mandate metadata staged: {staged_files}. "
                f"Next: commit (subject must start with mandate_id), then rerun preflight to validate."
            )
        else:
            files_list = ", ".join(f"[{path}]" for path in mandate_owned_paths)
            message = (
                f"Mandate-owned dirty files detected: {files_list}. "
                f"Use --stage to stage these files and metadata, then commit and rerun preflight."
            )

    log_lines.append(message)
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return RepairResult(
        kind=RepairKind.closeout_dirty_file_prep,
        success=not suspicious_paths,
        dry_run=dry_run,
        message=message,
        log_path=log_path,
    )


def repair_missing_mandate_metadata(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    dry_run: bool = False,
    allow_stash: bool = True,
    active_branch_override: str | None = None,
) -> RepairResult:
    finding = detect_missing_mandate_metadata(
        project_config,
        agent_state,
        active_branch_override=active_branch_override,
    )
    run_dir = get_agent_run_dir(project_config.name, str(agent_state["agent"]))
    log_path = run_dir / "repair_missing_mandate_metadata.log"
    log_lines = [
        "# Repair Missing Mandate Metadata",
        f"timestamp: {timestamp_utc()}",
    ]

    if finding is None:
        worktree = Path(str(agent_state.get("worktree", "")))
        slug = str(agent_state.get("slug", "")).strip()
        if worktree.exists() and slug and repo_expects_mandate_metadata(worktree):
            metadata = mandate_metadata_path(worktree, slug)
            if metadata.exists():
                field_repair = repair_mandate_metadata_fields(
                    project_config,
                    agent_state,
                    dry_run=dry_run,
                    active_branch_override=active_branch_override,
                )
                if field_repair.success:
                    if not dry_run:
                        staged_ok, staged_message = _stage_and_verify_mandate_metadata_files(
                            worktree,
                            slug,
                            dry_run=False,
                        )
                        log_lines.append(staged_message)
                        if not staged_ok:
                            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                            return RepairResult(
                                kind=RepairKind.mandate_metadata,
                                success=False,
                                dry_run=dry_run,
                                message=staged_message,
                                log_path=log_path,
                            )
                    return field_repair
                message = f"Mandate metadata exists but field repair failed: {field_repair.message}"
                log_lines.append(message)
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return RepairResult(
                    kind=RepairKind.mandate_metadata,
                    success=False,
                    dry_run=dry_run,
                    message=message,
                    log_path=log_path,
                )

        message = "No known safe repair detected for missing mandate metadata."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    log_lines.extend([
        f"worktree: {finding.worktree}",
        f"metadata_path: {finding.metadata_path}",
        f"canonical_mandate_path: {finding.canonical_mandate_path}",
    ])

    if not finding.can_repair or finding.repair_command is None:
        log_lines.append(finding.message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=False,
            dry_run=dry_run,
            message=finding.message,
            log_path=log_path,
        )

    log_lines.append(f"repair_command: {finding.repair_command}")
    if dry_run:
        log_lines.append("dry_run: true")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=True,
            dry_run=True,
            message="Dry run complete. Repair command is ready.",
            log_path=log_path,
        )

    stash_ref: str | None = None
    stash_message: str | None = None
    if allow_stash:
        stash_ref, stash_message = run_git_stash_if_dirty(finding.worktree)
        if stash_ref is not None:
            log_lines.append(f"stash_created: {stash_ref}")
    else:
        dirty = run_command("git status --porcelain", cwd=finding.worktree).stdout.strip()
        if dirty:
            message = (
                "Worktree is dirty and automatic stashing is disabled for this operation. "
                "Run `cascade repair <agent> --project <project>` to perform a safe stash-backed repair."
            )
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.missing_mandate_metadata,
                success=False,
                dry_run=False,
                message=message,
                log_path=log_path,
            )

    command_error: str | None = None
    try:
        run_command(finding.repair_command, cwd=finding.worktree)
    except CommandError as exc:
        command_error = str(exc)

    if stash_ref is not None:
        stash_ok, stash_error = restore_git_stash(finding.worktree, stash_ref)
        if not stash_ok:
            message = (
                "Repair command ran, but restoring stashed changes failed. "
                "Resolve conflicts in the worktree, then continue. The stash entry was not dropped."
            )
            log_lines.append(message)
            log_lines.append(stash_error or "")
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.missing_mandate_metadata,
                success=False,
                dry_run=False,
                message=message,
                log_path=log_path,
                stash_ref=stash_ref,
                stash_message=stash_message,
                stash_pop_conflict=True,
            )

    if command_error is not None:
        message = f"Repair command failed: {command_error}"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=False,
            dry_run=False,
            message=message,
            log_path=log_path,
            stash_ref=stash_ref,
            stash_message=stash_message,
        )

    ensured_ok, ensured_message, _ = _ensure_mandate_metadata_files_exist(
        project_config,
        agent_state,
        active_branch_override=active_branch_override,
        dry_run=False,
    )
    log_lines.append(ensured_message)
    if not ensured_ok:
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=False,
            dry_run=False,
            message=ensured_message,
            log_path=log_path,
            stash_ref=stash_ref,
            stash_message=stash_message,
        )

    if not finding.metadata_path.exists():
        message = (
            "Repair command and deterministic fallback completed but mandate metadata is still missing: "
            f"{finding.metadata_path}"
        )
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=False,
            dry_run=False,
            message=message,
            log_path=log_path,
            stash_ref=stash_ref,
            stash_message=stash_message,
        )

    field_repair = repair_mandate_metadata_fields(
        project_config,
        agent_state,
        dry_run=dry_run,
        active_branch_override=active_branch_override,
    )
    if not field_repair.success:
        message = f"Mandate metadata created, but canonical field repair failed: {field_repair.message}"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.mandate_metadata,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
            stash_ref=stash_ref,
            stash_message=stash_message,
        )

    staged_ok, staged_message = _stage_and_verify_mandate_metadata_files(
        finding.worktree,
        finding.slug,
        dry_run=False,
    )
    log_lines.append(staged_message)
    if not staged_ok:
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_mandate_metadata,
            success=False,
            dry_run=False,
            message=staged_message,
            log_path=log_path,
            stash_ref=stash_ref,
            stash_message=stash_message,
        )

    updated_state = dict(agent_state)
    updated_state["last_repair_kind"] = RepairKind.missing_mandate_metadata.value
    updated_state["last_repair_at"] = timestamp_utc()
    updated_state["mandate_metadata_present"] = True
    save_agent_state(project_config.name, str(agent_state["agent"]), updated_state)

    message = "Repair completed successfully."
    log_lines.append(message)
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return RepairResult(
        kind=RepairKind.missing_mandate_metadata,
        success=True,
        dry_run=False,
        message=message,
        log_path=log_path,
        stash_ref=stash_ref,
        stash_message=stash_message,
    )


def repair_missing_workspace_links(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    dry_run: bool = False,
) -> RepairResult:
    run_dir = get_agent_run_dir(project_config.name, str(agent_state["agent"]))
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "repair_missing_workspace_link.log"
    log_lines = [
        "# Repair Missing Workspace Link",
        f"timestamp: {timestamp_utc()}",
    ]

    if not project_config.workspace_links:
        message = "No workspace links configured. No repair is needed."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_workspace_link,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    workspace_root = resolve_workspace_root(project_config)
    if workspace_root is None:
        message = "workspace_links requires paths.workspace_root to be configured."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_workspace_link,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    try:
        resolved_links = resolve_workspace_link_paths(project_config)
    except ConfigError as exc:
        message = str(exc)
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.missing_workspace_link,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    created_count = 0
    for entry in resolved_links:
        link_path = entry.link_path
        target_path = entry.target_path
        log_lines.append(f"link: {link_path}")
        log_lines.append(f"target: {target_path}")

        if not is_inside_workspace(link_path, workspace_root, follow_symlinks=False):
            message = f"Link path escapes workspace_root: {link_path}"
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.missing_workspace_link,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        if not is_inside_workspace(target_path, workspace_root, follow_symlinks=True):
            message = f"Target path escapes workspace_root: {target_path}"
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.missing_workspace_link,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        if not target_path.exists():
            message = f"Workspace link target does not exist: {target_path}"
            log_lines.append(message)
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.missing_workspace_link,
                success=False,
                dry_run=dry_run,
                message=message,
                log_path=log_path,
            )

        if link_path.exists() or link_path.is_symlink():
            if not link_path.is_symlink():
                message = f"Workspace link path exists and is not a symlink: {link_path}"
                log_lines.append(message)
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return RepairResult(
                    kind=RepairKind.missing_workspace_link,
                    success=False,
                    dry_run=dry_run,
                    message=message,
                    log_path=log_path,
                )

            try:
                existing_target = link_path.resolve(strict=True)
            except FileNotFoundError:
                message = f"Workspace link points to missing target: {link_path}"
                log_lines.append(message)
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return RepairResult(
                    kind=RepairKind.missing_workspace_link,
                    success=False,
                    dry_run=dry_run,
                    message=message,
                    log_path=log_path,
                )

            expected_target = target_path.resolve()
            if existing_target != expected_target:
                message = (
                    f"Workspace link points elsewhere: {link_path} -> {existing_target} "
                    f"(expected {expected_target}). Human approval required."
                )
                log_lines.append(message)
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return RepairResult(
                    kind=RepairKind.missing_workspace_link,
                    success=False,
                    dry_run=dry_run,
                    message=message,
                    log_path=log_path,
                )

            log_lines.append("status: already-correct")
            continue

        if dry_run:
            log_lines.append("status: would-create")
            continue

        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(target_path.resolve())
        created_count += 1
        log_lines.append("status: created")

    message = (
        "Workspace link repair completed successfully."
        if created_count > 0
        else "Workspace links are already correct."
    )
    log_lines.append(message)
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return RepairResult(
        kind=RepairKind.missing_workspace_link,
        success=True,
        dry_run=dry_run,
        message=message,
        log_path=log_path,
    )


def _file_matches_any_pattern(file_path: str, patterns: list[str]) -> bool:
    """Check if file_path matches any glob pattern in the list."""
    for pattern in patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
    return False


def repair_dirty_tracked_file(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    file_path: str,
    dry_run: bool = False,
) -> RepairResult:
    """Repair a dirty file by reverting it with git checkout.
    
    Only reverts tracked files that match auto_revert_tracked and don't match never_revert.
    """
    from cascade.gates import is_file_tracked, is_file_dirty

    agent_name = str(agent_state.get("agent", "unknown"))
    run_dir = get_agent_run_dir(project_config.name, agent_name)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "repair_dirty_file.log"
    log_lines = [
        "# Repair Dirty File",
        f"timestamp: {timestamp_utc()}",
        f"file: {file_path}",
    ]

    worktree_str = agent_state.get("worktree")
    if not isinstance(worktree_str, str) or not worktree_str:
        message = "Agent state missing or invalid worktree."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    worktree = Path(worktree_str)
    if not worktree.exists():
        message = f"Worktree does not exist: {worktree}"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    # Check never_revert patterns first (highest priority)
    never_revert = project_config.dirty_file_repairs.never_revert
    if _file_matches_any_pattern(file_path, never_revert):
        message = f"File '{file_path}' matches never_revert patterns. Will not revert."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    # Check auto_revert_tracked patterns
    auto_revert_tracked = project_config.dirty_file_repairs.auto_revert_tracked
    if not _file_matches_any_pattern(file_path, auto_revert_tracked):
        message = f"File '{file_path}' does not match auto_revert_tracked patterns. Will not revert."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    # Check if file is tracked
    is_tracked = is_file_tracked(worktree, file_path)
    if not is_tracked:
        message = f"File '{file_path}' is not tracked by git. Will not attempt to revert."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    # Check if file is actually dirty
    is_dirty = is_file_dirty(worktree, file_path)
    if not is_dirty:
        message = f"File '{file_path}' is not dirty. No repair needed."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    if dry_run:
        message = f"Dry run: would revert '{file_path}' with git checkout"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    # Perform the actual git checkout
    try:
        run_command(f"git checkout -- {file_path}", cwd=worktree)
        message = f"Successfully reverted '{file_path}' with git checkout."
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=True,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )
    except CommandError as exc:
        message = f"Failed to revert '{file_path}': {exc}"
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.dirty_file,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )


def _extract_compose_project_names_from_log(log_text: str) -> list[str]:
    names: set[str] = set()
    patterns = [
        r"(?im)network\s+([a-z0-9][a-z0-9_-]*)_default",
        r"(?im)container\s+([a-z0-9][a-z0-9_-]*)-[a-z0-9_-]+-\d+",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, log_text):
            value = str(match.group(1)).strip()
            if value:
                names.add(value)
    return sorted(names)


def repair_docker_runtime_network(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    dry_run: bool = False,
    log_text: str | None = None,
) -> RepairResult:
    agent_name = str(agent_state.get("agent", "unknown"))
    run_dir = get_agent_run_dir(project_config.name, agent_name)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "repair_docker_runtime_network.log"

    worktree_str = agent_state.get("worktree")
    if not isinstance(worktree_str, str) or not worktree_str:
        return RepairResult(
            kind=RepairKind.docker_runtime_network,
            success=False,
            dry_run=dry_run,
            message="Agent state missing worktree for docker-runtime-network repair.",
            log_path=log_path,
        )

    worktree = Path(worktree_str)
    if not worktree.exists():
        return RepairResult(
            kind=RepairKind.docker_runtime_network,
            success=False,
            dry_run=dry_run,
            message=f"Worktree does not exist: {worktree}",
            log_path=log_path,
        )

    source_log_text = log_text
    if source_log_text is None:
        preflight_log = run_dir / "preflight.log"
        source_log_text = preflight_log.read_text(encoding="utf-8") if preflight_log.exists() else ""

    compose_projects = _extract_compose_project_names_from_log(source_log_text)
    log_lines = [
        "# Repair Docker Runtime Network",
        f"timestamp: {timestamp_utc()}",
        f"worktree: {worktree}",
        f"compose_projects: {', '.join(compose_projects) if compose_projects else '(none)'}",
    ]

    if not compose_projects:
        message = (
            "Unable to identify compose project name from failure log. "
            "Manual cleanup required for this worktree only."
        )
        log_lines.append(message)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.docker_runtime_network,
            success=False,
            dry_run=dry_run,
            message=message,
            log_path=log_path,
        )

    commands: list[str] = []
    for compose_project in compose_projects:
        quoted_project = shlex.quote(compose_project)
        commands.extend(
            [
                f"docker compose -p {quoted_project} down --remove-orphans",
                (
                    "ids=$(docker ps -a --filter "
                    f"label=com.docker.compose.project={quoted_project} -q); "
                    "if [[ -n \"$ids\" ]]; then docker rm -f $ids; fi"
                ),
                f"docker compose -p {quoted_project} down --remove-orphans",
            ]
        )

    for command in commands:
        log_lines.append(f"command: {command}")
        if dry_run:
            continue
        try:
            run_command(command, cwd=worktree)
        except CommandError as exc:
            log_lines.append(f"error: {exc}")
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            return RepairResult(
                kind=RepairKind.docker_runtime_network,
                success=False,
                dry_run=dry_run,
                message="docker-runtime-network cleanup failed for this worktree.",
                log_path=log_path,
            )

    message = (
        "Dry run: docker-runtime-network cleanup commands generated."
        if dry_run
        else "docker-runtime-network cleanup completed for this worktree."
    )
    log_lines.append(message)
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return RepairResult(
        kind=RepairKind.docker_runtime_network,
        success=True,
        dry_run=dry_run,
        message=message,
        log_path=log_path,
    )


def run_repair(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    kind: RepairKind,
    dry_run: bool,
    allow_stash: bool,
    active_branch_override: str | None,
    file_path: str | None = None,
    runtime_log_text: str | None = None,
) -> RepairResult:
    if kind == RepairKind.auto:
        finding = detect_missing_mandate_metadata(
            project_config,
            agent_state,
            active_branch_override=active_branch_override,
        )
        if finding is not None:
            return repair_missing_mandate_metadata(
                project_config,
                agent_state,
                dry_run=dry_run,
                allow_stash=allow_stash,
                active_branch_override=active_branch_override,
            )
        drift = _detect_mandate_metadata_field_drift(
            project_config,
            agent_state,
            active_branch_override=active_branch_override,
        )
        if drift:
            return repair_mandate_metadata_fields(
                project_config,
                agent_state,
                dry_run=dry_run,
                active_branch_override=active_branch_override,
            )
        if project_config.workspace_links:
            return repair_missing_workspace_links(
                project_config,
                agent_state,
                dry_run=dry_run,
            )

    if kind == RepairKind.missing_mandate_metadata:
        return repair_missing_mandate_metadata(
            project_config,
            agent_state,
            dry_run=dry_run,
            allow_stash=allow_stash,
            active_branch_override=active_branch_override,
        )

    if kind == RepairKind.mandate_metadata:
        return repair_mandate_metadata_fields(
            project_config,
            agent_state,
            dry_run=dry_run,
            active_branch_override=active_branch_override,
        )

    if kind == RepairKind.missing_workspace_link:
        return repair_missing_workspace_links(
            project_config,
            agent_state,
            dry_run=dry_run,
        )

    if kind == RepairKind.dirty_file:
        if not file_path:
            agent_name = str(agent_state.get("agent", "unknown"))
            run_dir = get_agent_run_dir(project_config.name, agent_name)
            log_path = run_dir / "repair_dirty_file.log"
            return RepairResult(
                kind=RepairKind.dirty_file,
                success=False,
                dry_run=dry_run,
                message="--file is required for --kind dirty-file",
                log_path=log_path,
            )
        return repair_dirty_tracked_file(
            project_config,
            agent_state,
            file_path=file_path,
            dry_run=dry_run,
        )

    if kind == RepairKind.closeout_dirty_file_prep:
        return prepare_mandate_closeout_dirty_files(
            project_config,
            agent_state,
            stage=not dry_run,
            commit=False,
            yes=False,
            dry_run=dry_run,
            commit_message=None,
        )

    if kind == RepairKind.docker_era_state:
        run_dir = get_agent_run_dir(project_config.name, str(agent_state["agent"]))
        log_path = run_dir / "repair_docker_era_state.log"
        stale = detect_docker_era_state(agent_state)
        if not stale:
            return RepairResult(
                kind=RepairKind.docker_era_state,
                success=True,
                dry_run=dry_run,
                message="No Docker-era state paths detected.",
                log_path=log_path,
            )

        migrated, changes = migrate_docker_era_state(agent_state, project_config=project_config)
        if not dry_run:
            save_agent_state(project_config.name, str(agent_state["agent"]), migrated)
        log_lines = [
            "# Repair Docker Era State",
            f"timestamp: {timestamp_utc()}",
            *changes,
        ]
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return RepairResult(
            kind=RepairKind.docker_era_state,
            success=True,
            dry_run=dry_run,
            message="Docker-era state migration completed.",
            log_path=log_path,
        )

    if kind == RepairKind.docker_runtime_network:
        return repair_docker_runtime_network(
            project_config,
            agent_state,
            dry_run=dry_run,
            log_text=runtime_log_text,
        )

    run_dir = get_agent_run_dir(project_config.name, str(agent_state["agent"]))
    log_path = run_dir / "repair_missing_mandate_metadata.log"
    return RepairResult(
        kind=kind,
        success=False,
        dry_run=dry_run,
        message="No known safe repair detected.",
        log_path=log_path,
    )


def maybe_initialize_mandate_metadata(
    project: ProjectConfig,
    *,
    worktree: Path,
    slug: str,
    agent: str,
    issue: int,
    title: str,
    active_branch_override: str | None = None,
) -> None:
    synthetic_state: dict[str, object] = {
        "project": project.name,
        "agent": agent,
        "slug": slug,
        "title": title,
        "issue": issue,
        "worktree": str(worktree),
    }
    finding = detect_missing_mandate_metadata(
        project,
        synthetic_state,
        active_branch_override=active_branch_override,
    )
    if finding is None:
        return
    if not finding.can_repair:
        print_warning(finding.message)
        return

    result = repair_missing_mandate_metadata(
        project,
        synthetic_state,
        dry_run=False,
        allow_stash=False,
        active_branch_override=active_branch_override,
    )
    if not result.success:
        raise CommandError(
            cmd=finding.repair_command or "(missing mandate command)",
            cwd=worktree,
            exit_code=1,
            output=result.message,
        )


def validate_mandate_metadata_before_preflight(
    project: ProjectConfig,
    worktree: Path,
    slug: str,
    *,
    agent: str,
    issue: int,
    title: str,
) -> None:
    synthetic_state: dict[str, object] = {
        "project": project.name,
        "agent": agent,
        "slug": slug,
        "title": title,
        "issue": issue,
        "worktree": str(worktree),
    }
    finding = detect_missing_mandate_metadata(project, synthetic_state)
    if finding is None:
        return

    if finding.can_repair:
        raise FileNotFoundError(
            "Required mandate metadata is missing: "
            f"{finding.metadata_path}. Repair available: cascade repair {agent} --project {project.name}"
        )

    raise FileNotFoundError(
        finding.message
    )


@app.command()
def claim(
    project_file: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    issue: int = typer.Option(..., min=1),
    agent: str = typer.Option(...),
    engine: str = typer.Option("opencode"),
    model: str | None = typer.Option(None),
) -> None:
    try:
        project = load_project_config(project_file)
        issue_payload = fetch_issue(project.github.owner, project.github.repo, issue)
    except (ConfigError, GithubError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    title = str(issue_payload.get("title", "")).strip()
    body = str(issue_payload.get("body", ""))
    issue_number = int(issue_payload.get("number", issue))
    slug = slugify(title)
    try:
        selected_model = model or default_model_name(project)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    branch = build_branch_name(project, agent, slug)

    try:
        _, run_dir = ensure_project_state_dirs(project.name, agent)
        create_worktree_command = format_command_template(
            project.commands.create_worktree,
            project=project,
            agent=agent,
            slug=slug,
            branch=branch,
            issue=issue_number,
            title=title,
        )
        run_command(create_worktree_command, cwd=project.paths.repo_root)
    except CommandError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    worktree_path, worktree_warning = find_worktree_path(project, agent, slug)
    is_valid_location, location_message = validate_worktree_location(project, worktree_path)
    if not is_valid_location:
        print_error(location_message)
        raise typer.Exit(1)

    mandate_path = run_dir / "mandate.md"
    launch_prompt_path = run_dir / "launch_prompt.md"
    mandate_path.write_text(body, encoding="utf-8")
    ensure_conversation_files(run_dir)

    try:
        maybe_initialize_mandate_metadata(
            project,
            worktree=worktree_path,
            slug=slug,
            agent=agent,
            issue=issue_number,
            title=title,
            active_branch_override=resolve_active_branch(project),
        )
    except CommandError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    metadata_path = mandate_metadata_path(worktree_path, slug)
    if metadata_path.exists():
        metadata_field_result = repair_mandate_metadata_fields(
            project,
            {
                "project": project.name,
                "agent": agent,
                "slug": slug,
                "worktree": str(worktree_path),
            },
            dry_run=False,
            active_branch_override=resolve_active_branch(project),
        )
        if not metadata_field_result.success:
            print_error(metadata_field_result.message)
            console.print(f"Repair log: {metadata_field_result.log_path}")
            raise typer.Exit(1)

    workspace_link_state: dict[str, object] = {
        "project": project.name,
        "agent": agent,
    }
    workspace_link_result = repair_missing_workspace_links(
        project,
        workspace_link_state,
        dry_run=False,
    )
    if not workspace_link_result.success:
        print_error(workspace_link_result.message)
        console.print(f"Repair log: {workspace_link_result.log_path}")
        raise typer.Exit(1)

    agent_state = {
        "project": project.name,
        "agent": agent,
        "issue": issue_number,
        "title": title,
        "slug": slug,
        "agent_branch": branch,
        "active_branch": resolve_active_branch(project),
        "repo": project.github.repo,
        "engine": engine,
        "model": selected_model,
        "state": AgentLifecycleState.claimed.value,
        "worktree": str(worktree_path),
        "run_dir": str(run_dir),
        "project_file": str(project_file.resolve()),
        "opencode_session_id": None,
        "last_mode": None,
        "last_interaction_at": None,
    }
    launch_prompt = build_launch_prompt(
        project=project,
        agent_state=agent_state,
        mandate_body=body,
        instruction_files=instruction_file_paths(project),
    )
    launch_prompt_path.write_text(launch_prompt, encoding="utf-8")
    save_agent_state(project.name, agent, agent_state)

    project_meta = read_github_project_config(worktree_path)
    if project_meta is not None:
        try:
            project_number = int(project_meta.get("project_number", 0) or 0)
        except (TypeError, ValueError):
            project_number = 0
        if project_number > 0:
            linked = get_project_item_for_issue(
                owner=project.github.owner,
                repo=project.github.repo,
                project_number=project_number,
                issue_number=issue_number,
            )
            if linked is None:
                print_warning("GitHub Project sync skipped: linked project item not found or token unavailable.")
            else:
                item_id = str(linked.get("item_id") or "")
                project_id = str(linked.get("project_id") or "")
                status_field_id = str(project_meta.get("status_field_id") or "")
                in_progress_option_id = str(project_meta.get("in_progress_option_id") or "")
                if item_id and project_id and status_field_id and in_progress_option_id:
                    status_ok = update_project_v2_item_status(
                        project_id=project_id,
                        item_id=item_id,
                        field_id=status_field_id,
                        option_id=in_progress_option_id,
                    )
                    if not status_ok:
                        print_warning("GitHub Project status sync failed (in_progress).")

                mandate_id = read_mandate_id(worktree_path, slug)
                mandate_id_field = str(project_meta.get("mandate_id_field_id") or "")
                if mandate_id and mandate_id_field and item_id and project_id:
                    text_ok = update_project_v2_text_field(
                        project_id=project_id,
                        item_id=item_id,
                        field_id=mandate_id_field,
                        value=mandate_id,
                    )
                    if not text_ok:
                        print_warning("GitHub Project Mandate ID sync failed.")

    console.print("[green]Claim created successfully.[/green]")
    console.print(f"Issue: #{issue_number}")
    console.print(f"Title: {title}")
    console.print(f"Slug: {slug}")
    console.print(f"Worktree: {worktree_path}")
    console.print(f"Launch prompt: {launch_prompt_path}")
    if worktree_warning is not None:
        console.print(
            f"[yellow]Warning:[/yellow] {worktree_warning} State was saved with the selected location."
        )
    for warning in validate_instruction_files(project):
        print_warning(warning)


@app.command(help="Claim issue, prepare implementation context, and launch OpenCode.")
def start(
    issue: int = typer.Argument(..., min=1),
    agent: str = typer.Option(...),
    project_file: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    profile: str | None = typer.Option(None, help="Optional model profile for implementation cost estimate."),
    task: str = typer.Option("implement", help=f"Task type: {', '.join(sorted(ALLOWED_TASKS))}"),
    no_launch: bool = typer.Option(False, "--no-launch", help="Skip launching OpenCode after setup."),
    engine: str = typer.Option("opencode"),
    model: str | None = typer.Option(None),
) -> None:
    if task not in ALLOWED_TASKS:
        print_error(f"Unknown task '{task}'. Allowed: {', '.join(sorted(ALLOWED_TASKS))}")
        raise typer.Exit(1)

    # Step 1: claim issue and initialize state/worktree.
    claim(
        project_file=project_file,
        issue=issue,
        agent=agent,
        engine=engine,
        model=model,
    )

    try:
        project_config = load_project_config(project_file)
        agent_state = load_agent_state(project_config.name, agent)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(
            f"{exc} Run the configured create_worktree command or use `cascade claim` to recreate state."
        )
        raise typer.Exit(1) from exc

    emit_standards_warnings(project_config, agent_state, worktree)

    run_dir = get_agent_run_dir(project_config.name, agent)

    # Step 2: build deterministic task context pack.
    try:
        pack = build_context_pack(project_config, agent_state, task, run_dir)
        context_md, context_json = save_context_pack(run_dir, pack)
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    console.print(f"Prepared context: {context_md}")
    console.print(f"Context metadata: {context_json}")

    # Step 3: estimate cost when profile support is available.
    selected_profile = profile
    if selected_profile is None:
        task_profile = resolve_model_for_task(project_config, task)
        if task_profile is not None:
            selected_profile = builtins.next(
                (name for name, value in project_config.models.profiles.items() if value == task_profile),
                None,
            )

    if selected_profile is not None:
        try:
            model_profile = get_model_profile(project_config, selected_profile)
            input_tokens = pack.estimated_input_tokens
            output_tokens = DEFAULT_EXPECTED_OUTPUT_TOKENS.get(task, 10000)
            for line in cost_summary_lines(input_tokens, output_tokens, model_profile, selected_profile):
                console.print(line)
        except ConfigError as exc:
            print_warning(str(exc))

    if selected_profile is not None:
        prepare_model_call(
            agent=agent,
            project=project_config.name,
            task=task,
            profile=selected_profile,
            include_diff=False,
        )
        model_call_meta_path = run_dir / f"{task}_model_call.json"
        prompt_path = run_dir / f"{task}_prompt.md"
        if model_call_meta_path.exists():
            metadata = json.loads(model_call_meta_path.read_text(encoding="utf-8"))
            console.print(f"Worktree      : {agent_state['worktree']}")
            console.print(f"Prompt file   : {prompt_path}")
            console.print(f"Profile       : {selected_profile}")
            console.print(f"Model         : {metadata.get('model_id', '(unknown)')}")
            console.print(f"Est. cost USD : {metadata.get('estimated_cost_usd', '(unknown)')}")

    if no_launch:
        console.print("[green]Start complete (no launch).[/green]")
        console.print(f"Next: cascade run-agent {agent} --project {project_config.name}")
        return

    # Step 4: launch OpenCode in the assigned worktree.
    task_prompt = run_dir / f"{task}_prompt.md"
    launch_task = task if task_prompt.exists() else None
    launch_prompt_path = task_prompt if launch_task is not None else run_dir / "launch_prompt.md"
    try:
        run_agent(
            agent=agent,
            project=project_config.name,
            print_prompt=False,
            task=launch_task,
        )
    except Exception as exc:
        print_warning(f"Auto-launch failed after setup: {exc}")
        console.print("[yellow]Start setup completed, but OpenCode did not launch automatically.[/yellow]")
        console.print(f"Worktree: {agent_state['worktree']}")
        console.print(f"Prompt file: {launch_prompt_path}")
        console.print(f"Manual launch: cascade run-agent {agent} --project {project_config.name}")
        console.print(f"Loop alternative: cascade loop {agent} --project {project_config.name} --watch")
        return


@app.command(help="Show diff summary, run preflight, and recommend finish or fix.")
def check(
    agent: str,
    project: str = typer.Option(...),
    repair: bool = typer.Option(False, "--repair"),
    repair_only: bool = typer.Option(False, "--repair-only"),
    verbose: bool = typer.Option(False, "--verbose"),
    watch: bool = typer.Option(False, "--watch"),
    active_branch: str | None = typer.Option(None, "--active-branch"),
    auto_fix: bool = typer.Option(False, "--auto-fix"),
) -> None:
    if auto_fix:
        options = _resolve_loop_options(2, 1, 1.0, False, None, verbose, watch, None, None, None, None)
        result = run_auto_repair_loop(
            project=project,
            agent=agent,
            options=options,
            active_branch=active_branch,
        )
        if result.get("status") == "passed":
            return
        raise typer.Exit(1)

    if repair_only:
        repair = True

    diff(agent=agent, project=project, save=True)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(project_file_path)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    metadata_errors = validate_mandate_metadata(
        worktree=worktree,
        slug=str(agent_state.get("slug", "")),
        agent=agent,
        active_branch=resolve_active_branch(project_config, active_branch_override=active_branch),
        project_config=project_config,
        repo_name=project_config.github.repo,
        expected_worktree_path=worktree,
    )
    for item in metadata_errors:
        print_warning(f"Mandate metadata validation: {item}")

    finding = detect_missing_mandate_metadata(
        project_config,
        agent_state,
        active_branch_override=active_branch,
    )
    if finding is not None:
        if repair:
            repair_result = repair_missing_mandate_metadata(
                project_config,
                agent_state,
                dry_run=False,
                allow_stash=True,
                active_branch_override=active_branch,
            )
            console.print(f"Repair log: {repair_result.log_path}")
            if not repair_result.success:
                print_error(repair_result.message)
                raise typer.Exit(1)
            console.print("Repair completed.")
            if repair_only:
                console.print(f"Next: cascade check {agent} --project {project}")
                return
        else:
            print_error(finding.message)
            console.print(f"Repair available: cascade repair {agent} --project {project}")
            console.print(f"Or run: cascade check {agent} --project {project} --repair")
            raise typer.Exit(1)

    workspace_link_result = repair_missing_workspace_links(
        project_config,
        agent_state,
        dry_run=False,
    )
    if not workspace_link_result.success:
        print_error(workspace_link_result.message)
        console.print(f"Repair log: {workspace_link_result.log_path}")
        console.print(
            f"Repair available: cascade repair {agent} --project {project} --kind missing-workspace-link"
        )
        raise typer.Exit(1)

    preflight_exit_code = 0
    try:
        if verbose or watch:
            preflight(agent=agent, project=project, verbose=verbose, watch=watch)
        else:
            preflight(agent=agent, project=project)
    except typer.Exit as exc:
        preflight_exit_code = int(exc.exit_code or 1)

    run_dir = get_agent_run_dir(project, agent)
    gate_result = load_gate_result(run_dir)
    if gate_result is None:
        print_error("No gate result found after preflight. Check the preflight log.")
        raise typer.Exit(preflight_exit_code or 1)

    if gate_result.get("passed"):
        console.print("[green]Preflight passed.[/green]")
        console.print(f"Next: cascade finish {agent} --project {project}")
        return

    gate_log_path = Path(str(gate_result.get("log_path", run_dir / "preflight.log")))
    gate_log_tail = _load_log_tail(gate_log_path)
    gate_classification = classify_gate_failure(gate_log_tail)
    if str(gate_classification.get("hook") or "") == "docker-runtime-network":
        repair_result = run_repair(
            project_config,
            agent_state,
            kind=RepairKind.docker_runtime_network,
            dry_run=False,
            allow_stash=True,
            active_branch_override=active_branch,
            runtime_log_text=gate_log_tail,
        )
        console.print(f"Repair log: {repair_result.log_path}")
        if repair_result.success:
            console.print("[yellow]Deterministic docker-runtime-network repair applied; retrying preflight once.[/yellow]")
            try:
                if verbose or watch:
                    preflight(agent=agent, project=project, verbose=verbose, watch=watch)
                else:
                    preflight(agent=agent, project=project)
            except typer.Exit as exc:
                preflight_exit_code = int(exc.exit_code or 1)

            gate_result = load_gate_result(run_dir)
            if gate_result is not None and gate_result.get("passed"):
                console.print("[green]Preflight passed after deterministic docker-runtime-network retry.[/green]")
                console.print(f"Next: cascade finish {agent} --project {project}")
                return

            gate_result = load_gate_result(run_dir)
            if gate_result is not None:
                retry_log_path = Path(str(gate_result.get("log_path", run_dir / "preflight.log")))
                retry_classification = classify_gate_failure(_load_log_tail(retry_log_path))
                if str(retry_classification.get("hook") or "") == "docker-runtime-network":
                    print_error("docker-runtime-network persisted after deterministic repair/retry.")
        else:
            print_warning(f"Deterministic docker-runtime-network repair failed: {repair_result.message}")

    gate_summary(agent=agent, project=project)
    console.print(f"Next: cascade fix {agent} --project {project} --profile debugger")
    raise typer.Exit(preflight_exit_code or 1)


@app.command(help="Repair known safe workflow setup failures without invoking models.")
def repair(
    agent: str,
    project: str = typer.Option(...),
    kind: RepairKind = typer.Option(RepairKind.auto, "--kind"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes"),
    active_branch: str | None = typer.Option(None, "--active-branch"),
    file: str | None = typer.Option(None, "--file", help="File path for --kind dirty-file repairs."),
) -> None:
    del yes
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(project_file_path)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    if kind not in {RepairKind.missing_workspace_link, RepairKind.dirty_file, RepairKind.closeout_dirty_file_prep}:
        workspace_link_result = repair_missing_workspace_links(
            project_config,
            agent_state,
            dry_run=dry_run,
        )
        if not workspace_link_result.success:
            console.print(f"Repair log: {workspace_link_result.log_path}")
            print_error(workspace_link_result.message)
            raise typer.Exit(1)

    result = run_repair(
        project_config,
        agent_state,
        kind=kind,
        dry_run=dry_run,
        allow_stash=True,
        active_branch_override=active_branch,
        file_path=file,
    )
    console.print(f"Repair log: {result.log_path}")
    if not result.success:
        print_error(result.message)
        raise typer.Exit(1)
    console.print(result.message)
    console.print(f"Next: cascade check {agent} --project {project}")


@app.command(name="closeout-prep", help="Inspect mandate dirty files, stage mandate-owned files, and optionally create a mandate-id-prefixed commit.")
def closeout_prep(
    agent: str,
    project: str = typer.Option(...),
    stage: bool = typer.Option(True, "--stage/--no-stage"),
    commit: bool = typer.Option(False, "--commit"),
    auto_fix_gates: bool = typer.Option(False, "--auto-fix-gates", help="When commit fails on a model-fixable gate, run headless gate-fix and retry commit once."),
    yes: bool = typer.Option(False, "--yes"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    message: str | None = typer.Option(None, "--message", help="Commit message body. mandate_id prefix is enforced."),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(project_file_path)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    result, retried = _execute_closeout_prep_flow(
        agent=agent,
        project=project,
        project_config=project_config,
        agent_state=agent_state,
        stage=stage,
        commit=commit,
        auto_fix_gates=auto_fix_gates,
        yes=yes,
        dry_run=dry_run,
        message=message,
    )
    console.print(f"Closeout prep log: {result.log_path}")
    if retried:
        console.print(f"Closeout prep retry log: {result.log_path}")
    if result.success:
        console.print(result.message)
        if not commit:
            console.print(f"Next: cascade preflight {agent} --project {project}")
        return

    print_error(result.message)
    console.print(
        f"Suggested next step: cascade closeout-prep {agent} --project {project} --stage"
    )
    raise typer.Exit(1)


def _execute_closeout_prep_flow(
    *,
    agent: str,
    project: str,
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    stage: bool,
    commit: bool,
    auto_fix_gates: bool,
    yes: bool,
    dry_run: bool,
    message: str | None,
) -> tuple[RepairResult, bool]:
    result = prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=stage,
        commit=commit,
        yes=yes,
        dry_run=dry_run,
        commit_message=message,
    )
    if result.success:
        return result, False

    if not (auto_fix_gates and commit and not dry_run):
        return result, False

    run_dir = get_agent_run_dir(project, agent)
    active_failure = _resolve_gate_fix_failure_source(run_dir=run_dir, explicit_context_file=None)
    if active_failure is None:
        return result, False

    failure_source = str(active_failure.get("source") or "")
    if failure_source != "closeout-prep-commit":
        return result, False

    console.print(
        f"[yellow]Commit gate failed. Running headless gate-fix from source:[/yellow] {failure_source or 'unknown'}"
    )
    try:
        gate_fix(
            agent=agent,
            project=project,
            profile=None,
            max_attempts=3,
            max_estimated_cost=0.25,
            fallback_model=None,
            stream=True,
            debug=False,
            failure_context_file=None,
        )
    except typer.Exit as exc:
        exit_code = 1 if exc.exit_code is None else int(exc.exit_code)
        if exit_code != 0:
            raise

    retry_result = prepare_mandate_closeout_dirty_files(
        project_config,
        agent_state,
        stage=stage,
        commit=commit,
        yes=yes,
        dry_run=dry_run,
        commit_message=message,
    )
    return retry_result, True


def _resolve_loop_options(
    max_iterations: int | None,
    max_model_fixes: int | None,
    max_estimated_cost: float | None,
    dry_run: bool,
    non_interactive: bool | None,
    verbose: bool,
    watch: bool,
    profile: str | None,
    cheap_profile: str | None,
    debug_profile: str | None,
    executor_profile: str | None,
) -> LoopRunOptions:
    return LoopRunOptions(
        max_iterations=max_iterations,
        max_model_fixes=max_model_fixes,
        max_estimated_cost_usd=max_estimated_cost,
        dry_run=dry_run,
        non_interactive=non_interactive,
        verbose=verbose,
        watch=watch,
        profile=profile,
        cheap_profile=cheap_profile or "cheap_coder",
        debug_profile=debug_profile or "debugger",
        executor_profile=executor_profile or "executor",
        stop_on_same_failure_twice=None,
    )


def _resolve_loop_non_interactive_mode(non_interactive: bool | None) -> tuple[bool, str | None]:
    if non_interactive is False:
        return (
            False,
            "Interactive OpenCode will not auto-exit; press Ctrl+C when the fix attempt is complete so Cascade can continue.",
        )

    supported, reason = supports_non_interactive_run()
    if supported:
        return True, None

    if non_interactive is True:
        detail = reason or "OpenCode non-interactive mode is unavailable."
        raise OpenCodeError(f"--non-interactive requested, but non-interactive OpenCode is unavailable: {detail}")

    detail = reason or "OpenCode non-interactive mode is unavailable."
    raise OpenCodeError(
        "OpenCode non-interactive mode is unavailable; loop defaults to automation-safe mode and will not launch "
        f"interactive TUI automatically. Details: {detail}. Use --interactive to opt into TUI mode."
    )


def _print_opencode_log_tail(log_path: Path, *, max_lines: int = 80) -> None:
    if not log_path.exists():
        return
    lines = log_path.read_text(encoding="utf-8").splitlines()
    for line in lines[-max_lines:]:
        console.print(f"[opencode] {line}", markup=False)


def _print_prefixed_lines(prefix: str, lines: list[str]) -> None:
    for line in lines:
        console.print(f"[{prefix}] {line}", markup=False)


def _unique_tail_lines(lines: list[str], limit: int) -> list[str]:
    selected: list[str] = []
    last_seen: str | None = None
    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped:
            continue
        if stripped == last_seen:
            continue
        selected.append(stripped)
        last_seen = stripped
    if len(selected) <= limit:
        return selected
    return selected[-limit:]


def _build_preflight_log_content(timestamp: str, command: str, exit_code: int | str, output: str) -> str:
    return (
        f"# Preflight Run\n"
        f"timestamp: {timestamp}\n"
        f"command: {command}\n"
        f"exit_code: {exit_code}\n\n"
        f"{output}"
    )


def _failure_context_path(run_dir: Path, filename: str) -> Path:
    return run_dir / filename


def _save_failure_context(run_dir: Path, filename: str, payload: dict[str, object]) -> Path:
    path = _failure_context_path(run_dir, filename)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _clear_failure_context(run_dir: Path, filename: str) -> None:
    _failure_context_path(run_dir, filename).unlink(missing_ok=True)


def _load_failure_context(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    command = payload.get("command")
    log_text = payload.get("log")
    if not isinstance(command, str) or not command.strip():
        return None
    if not isinstance(log_text, str) or not log_text.strip():
        return None
    return payload


def _build_preflight_failure_context(run_dir: Path) -> dict[str, object] | None:
    gate_result = load_gate_result(run_dir)
    if gate_result is None:
        return None
    if bool(gate_result.get("passed")):
        return None

    log_path = Path(str(gate_result.get("log_path", run_dir / "preflight.log")))
    try:
        log_text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    touched_raw = gate_result.get("touched_files", [])
    touched_files = [str(item) for item in touched_raw if isinstance(item, str)] if isinstance(touched_raw, list) else []
    hook = str(gate_result.get("hook") or classify_gate_failure(log_text).get("hook") or "unknown")
    return {
        "source": "preflight",
        "timestamp": str(gate_result.get("timestamp") or ""),
        "command": str(gate_result.get("command") or "make preflight"),
        "hook": hook,
        "log": log_text,
        "log_path": str(log_path),
        "touched_files": touched_files,
        "gate_result": gate_result,
    }


def _build_gate_result_fallback_context(run_dir: Path) -> dict[str, object] | None:
    gate_result = load_gate_result(run_dir)
    if gate_result is None:
        return None

    log_path = Path(str(gate_result.get("log_path", run_dir / "preflight.log")))
    try:
        log_text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    touched_raw = gate_result.get("touched_files", [])
    touched_files = [str(item) for item in touched_raw if isinstance(item, str)] if isinstance(touched_raw, list) else []
    hook = str(gate_result.get("hook") or classify_gate_failure(log_text).get("hook") or "unknown")
    return {
        "source": "stored-gate-result",
        "timestamp": str(gate_result.get("timestamp") or ""),
        "command": str(gate_result.get("command") or "make preflight"),
        "hook": hook,
        "log": log_text,
        "log_path": str(log_path),
        "touched_files": touched_files,
        "gate_result": gate_result,
    }


def _resolve_gate_fix_failure_source(
    *,
    run_dir: Path,
    explicit_context_file: Path | None,
) -> dict[str, object] | None:
    if explicit_context_file is not None:
        explicit_payload = _load_failure_context(explicit_context_file)
        if explicit_payload is not None:
            explicit = dict(explicit_payload)
            explicit["source"] = "explicit-context-file"
            return explicit

    commit_payload = _load_failure_context(_failure_context_path(run_dir, _COMMIT_FAILURE_CONTEXT_FILENAME))
    if commit_payload is not None:
        return commit_payload

    preflight_payload = _build_preflight_failure_context(run_dir)
    if preflight_payload is not None:
        return preflight_payload

    closeout_payload = _load_failure_context(_failure_context_path(run_dir, _CLOSEOUT_FAILURE_CONTEXT_FILENAME))
    if closeout_payload is not None:
        return closeout_payload

    current_payload = _load_failure_context(_failure_context_path(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME))
    if current_payload is not None:
        return current_payload

    return _build_gate_result_fallback_context(run_dir)


def _run_preflight_command(
    *,
    command: str,
    worktree: Path,
    log_path: Path,
    verbose: bool,
    watch: bool,
) -> PreflightRunResult:
    if not verbose and not watch:
        result = subprocess.run(
            command,
            cwd=worktree,
            shell=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return PreflightRunResult(returncode=result.returncode, output=result.stdout or "")

    preflight_timestamp = timestamp_utc()
    log_path.write_text(
        _build_preflight_log_content(preflight_timestamp, command, "(running)", ""),
        encoding="utf-8",
    )
    console.print(f"[preflight] running: {command}", markup=False)

    process = subprocess.Popen(
        command,
        cwd=worktree,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("Preflight process did not expose stdout.")

    output_chunks: list[str] = []
    display_lines: list[str] = []
    line_queue: queue.Queue[str | None] = queue.Queue()
    reader_done = threading.Event()

    def _reader() -> None:
        try:
            for chunk in process.stdout:
                line_queue.put(chunk)
        finally:
            reader_done.set()
            line_queue.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    started_at = time.monotonic()
    last_verbose_index = 0
    last_progress_update = started_at

    while True:
        while True:
            try:
                item = line_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            output_chunks.append(item)
            display_lines.extend(item.splitlines())
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(item)
            if watch:
                _print_prefixed_lines("preflight", [line.rstrip("\n") for line in item.splitlines()])

        returncode = process.poll()
        now = time.monotonic()
        interval_elapsed = now - last_progress_update >= _PREFLIGHT_PROGRESS_INTERVAL_SECONDS
        if interval_elapsed and returncode is None:
            total_elapsed_seconds = max(1, int(now - started_at))
            console.print(f"[preflight] still running... {total_elapsed_seconds}s elapsed", markup=False)
            if verbose and not watch:
                new_lines = display_lines[last_verbose_index:]
                tail_lines = _unique_tail_lines(new_lines, _PREFLIGHT_VERBOSE_TAIL_LINES)
                if tail_lines:
                    _print_prefixed_lines("preflight", tail_lines)
                last_verbose_index = len(display_lines)
            last_progress_update = now

        if returncode is not None and reader_done.is_set() and line_queue.empty():
            reader.join(timeout=0.1)
            output = "".join(output_chunks)
            log_path.write_text(
                _build_preflight_log_content(preflight_timestamp, command, returncode, output),
                encoding="utf-8",
            )
            elapsed_seconds = max(1, int(time.monotonic() - started_at))
            console.print(f"[preflight] completed with exit code {returncode} in {elapsed_seconds}s", markup=False)
            return PreflightRunResult(returncode=returncode, output=output)

        time.sleep(0.1)


def _emit_preflight_failure_tail(output: str) -> None:
    tail_lines = _unique_tail_lines(output.splitlines(), _PREFLIGHT_FAILURE_TAIL_LINES)
    if not tail_lines:
        return
    console.print("[preflight] last log lines before failure:", markup=False)
    _print_prefixed_lines("preflight", tail_lines)


_OPAQUE_PREFLIGHT_LINE_RE = re.compile(
    r"^\s*make(?:\[\d+\])?:\s*\*\*\*\s*\[.*?\]\s*Error\s*\d+\s*$"
)


def _is_opaque_preflight_log(output: str) -> bool:
    """Return True when the captured output contains only make error-wrapper lines.

    A thin log like 'make: *** [mandate-preflight] Error 1' gives the user no
    actionable information about the real failure.  Detecting this case lets the
    caller surface a targeted recommendation.
    """
    meaningful_lines = [
        line
        for line in output.splitlines()
        if line.strip() and not _OPAQUE_PREFLIGHT_LINE_RE.match(line)
    ]
    return not meaningful_lines


def _run_configured_gate_fix(
    project_config: ProjectConfig,
    hook: str,
    *,
    worktree: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    gate_fix = project_config.gate_fixes.get(hook)
    if gate_fix is not None and not gate_fix.model_required:
        if dry_run:
            return True, f"Dry run: would execute configured gate fix command: {gate_fix.command}"
        run_command(gate_fix.command, cwd=worktree)
        return True, f"Executed configured gate fix command for '{hook}'."

    command = project_config.autofix_commands.get(hook)
    if command is not None:
        if dry_run:
            return True, f"Dry run: would execute configured autofix command: {command}"
        run_command(command, cwd=worktree)
        return True, f"Executed configured autofix command for '{hook}'."

    return False, "No configured deterministic autofix command for this failure."


def _run_closeout_dirty_file_repair(
    project_config: ProjectConfig,
    *,
    worktree: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    command = project_config.commands.closeout_dirty_file
    if command is None:
        return False, "dirty_file_requires_closeout_action"
    if dry_run:
        return True, f"Dry run: would execute configured closeout command: {command}"
    run_command(command, cwd=worktree)
    return True, f"Executed configured closeout dirty-file command: {command}"


def _run_mandate_metadata_repair(
    project_config: ProjectConfig,
    *,
    worktree: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    del project_config
    del worktree
    del dry_run
    return False, "mandate_metadata_requires_closeout_action"


def _run_model_fix_attempt(
    *,
    project: str,
    agent: str,
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    options: LoopRunOptions,
    run_dir: Path,
    iteration: int,
    classification: dict[str, object],
    log_tail: str,
    gate_result: dict[str, object],
    non_interactive: bool,
) -> tuple[bool, str, float, str | None]:
    category = str(classification.get("category", "unknown"))
    hook = str(classification.get("hook") or "(unknown)")

    model_profile_name = _select_model_profile_for_category(category, options, project_config)
    model_profile = get_model_profile(project_config, model_profile_name)

    worktree = Path(str(agent_state["worktree"]))
    touched_raw = gate_result.get("touched_files", [])
    touched_files = list(touched_raw) if isinstance(touched_raw, list) else []
    prompt = _build_loop_fix_prompt(
        category=category,
        hook=hook,
        log_tail=log_tail,
        touched_files=touched_files,
        diff_stat=get_git_diff_stat(worktree),
    )
    prompt_path = run_dir / f"loop_fix_prompt_{iteration}.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    input_tokens = estimate_tokens(prompt)
    expected_output_tokens = int(project_config.repair_loop.default_expected_output_tokens.get("fix", 12000))
    estimated_cost = estimate_cost(input_tokens, expected_output_tokens, model_profile)

    if options.dry_run:
        return True, f"Dry run: would run model fix with profile '{model_profile_name}'.", estimated_cost, model_profile_name

    if non_interactive:
        ensure_opencode_available()
        opencode_log_path = run_dir / f"loop_opencode_{iteration}.log"
        console.print("[loop] launching OpenCode", markup=False)
        try:
            if options.watch:
                try:
                    opencode_result = run_prompt_streaming(
                        prompt=prompt,
                        worktree=worktree,
                        model=model_id_for_opencode(model_profile),
                        mode=OpenCodeMode.build,
                        use_continue=True,
                        log_path=opencode_log_path,
                        on_line=lambda line: console.print(f"[opencode] {line}", markup=False),
                    )
                except OpenCodeError:
                    console.print(
                        "[loop] live streaming unavailable; replaying saved OpenCode log after completion",
                        markup=False,
                    )
                    opencode_result = run_prompt_with_result(
                        prompt=prompt,
                        worktree=worktree,
                        model=model_id_for_opencode(model_profile),
                        mode=OpenCodeMode.build,
                        use_continue=True,
                    )
                    opencode_log_path.write_text(
                        "STDOUT:\n"
                        f"{opencode_result.stdout}\n"
                        "STDERR:\n"
                        f"{opencode_result.stderr}\n",
                        encoding="utf-8",
                    )
                    _print_opencode_log_tail(opencode_log_path)
            else:
                opencode_result = run_prompt_with_result(
                    prompt=prompt,
                    worktree=worktree,
                    model=model_id_for_opencode(model_profile),
                    mode=OpenCodeMode.build,
                    use_continue=True,
                )
                opencode_log_path.write_text(
                    "STDOUT:\n"
                    f"{opencode_result.stdout}\n"
                    "STDERR:\n"
                    f"{opencode_result.stderr}\n",
                    encoding="utf-8",
                )
        except OSError as exc:
            return False, f"Model session failed: {exc}", estimated_cost, model_profile_name

        console.print(f"[loop] OpenCode exited with code {opencode_result.returncode}", markup=False)
        if opencode_result.returncode != 0:
            return False, "open_code_exit_nonzero", estimated_cost, model_profile_name
    else:
        print_warning(
            "Interactive OpenCode will not auto-exit; press Ctrl+C when the fix attempt is complete so Cascade can continue."
        )
        try:
            run_agent(
                agent=agent,
                project=project,
                print_prompt=False,
                with_prompt=True,
                non_interactive=False,
                prompt_file=prompt_path,
                task=None,
                mode=OpenCodeMode.build,
            )
        except typer.Exit as exc:
            return (
                False,
                f"Model session exited unsuccessfully (exit={int(exc.exit_code or 1)}).",
                estimated_cost,
                model_profile_name,
            )

    increment_attempt(project, agent, "fix", profile=model_profile_name)
    return True, f"Executed model-backed fix attempt with profile '{model_profile_name}'.", estimated_cost, model_profile_name


def run_auto_repair_loop(
    *,
    project: str,
    agent: str,
    options: LoopRunOptions,
    active_branch: str | None,
) -> dict[str, object]:
    agent_state = load_agent_state(project, agent)

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        raise ValueError("Agent state does not include project_file.")
    project_config = load_project_config(project_file_path)

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    if project_config.commands.preflight is None:
        raise ValueError("Project config does not define commands.preflight.")

    worktree = require_existing_worktree(agent_state)
    is_valid_location, location_message = validate_worktree_location(project_config, worktree)
    if not is_valid_location:
        raise ValueError(location_message)

    max_iterations = options.max_iterations or project_config.repair_loop.max_iterations
    max_model_fixes = options.max_model_fixes or project_config.repair_loop.max_model_fixes
    max_budget = (
        options.max_estimated_cost_usd
        if options.max_estimated_cost_usd is not None
        else project_config.repair_loop.max_estimated_cost_usd
    )

    run_dir = get_agent_run_dir(project, agent)
    metadata_path = _repair_loop_metadata_path(project, agent)
    started_at = timestamp_utc()
    loop_metadata: dict[str, object] = {
        "status": "running",
        "started_at": started_at,
        "updated_at": started_at,
        "iteration": 0,
        "iterations": 0,
        "max_iterations": max_iterations,
        "deterministic_repairs_used": 0,
        "model_fix_attempts": 0,
        "model_fixes_used": 0,
        "max_model_fixes": max_model_fixes,
        "estimated_cost_spent": 0.0,
        "estimated_cost_used": 0.0,
        "max_estimated_cost": max_budget,
        "profiles_used": [],
        "model_profiles_used": [],
        "failure_signatures": [],
        "last_failure_category": None,
        "last_failure_hook": None,
        "last_repair_kind": None,
        "last_repair_result": None,
        "last_log_path": None,
        "preflight_log_paths": [],
        "last_action": "initialized",
        "stop_reason": None,
        "next_action": "run_preflight",
    }
    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")

    stop_on_same_failure_twice = project_config.repair_loop.stop_on_same_failure_twice
    if options.stop_on_same_failure_twice is not None:
        stop_on_same_failure_twice = options.stop_on_same_failure_twice

    approval_categories = set(project_config.repair_loop.require_approval_categories)

    last_signature = ""
    repeat_count = 0
    last_deterministic_repair_kind: str | None = None
    last_deterministic_repair_signature: str | None = None
    loop_non_interactive: bool | None = None
    loop_mode_warning_printed = False
    validation_slot_timeouts = 0
    docker_runtime_network_repairs = 0

    try:
        for iteration in range(1, max_iterations + 1):
            loop_metadata["iteration"] = iteration
            loop_metadata["iterations"] = iteration
            loop_metadata["last_action"] = "run_preflight"
            loop_metadata["next_action"] = "run_preflight"
            loop_metadata["updated_at"] = timestamp_utc()
            metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
            if options.verbose or options.watch:
                console.print(f"[loop] preflight attempt {iteration}/{max_iterations}", markup=False)

            branch_error = get_agent_branch_mismatch_error(
                project_config,
                worktree=worktree,
                agent=agent,
                slug=str(agent_state.get("slug", "")),
            )
            if branch_error is not None:
                loop_metadata["status"] = "needs_human"
                loop_metadata["last_action"] = "agent_branch_mismatch"
                loop_metadata["stop_reason"] = "mandate-agent-branch-mismatch"
                loop_metadata["next_action"] = branch_error
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            metadata_finding = detect_missing_mandate_metadata(
                project_config,
                agent_state,
                active_branch_override=active_branch,
            )
            if metadata_finding is not None:
                repair_result = repair_missing_mandate_metadata(
                    project_config,
                    agent_state,
                    dry_run=options.dry_run,
                    allow_stash=True,
                    active_branch_override=active_branch,
                )
                if not repair_result.success:
                    loop_metadata["status"] = "stopped"
                    loop_metadata["last_action"] = "repair_failed"
                    loop_metadata["last_repair_kind"] = RepairKind.missing_mandate_metadata.value
                    loop_metadata["last_repair_result"] = "failed"
                    loop_metadata["stop_reason"] = repair_result.message
                    loop_metadata["next_action"] = f"inspect_log:{repair_result.log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata
                loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                loop_metadata["last_repair_kind"] = repair_result.kind.value
                loop_metadata["last_repair_result"] = "success"
                loop_metadata["last_action"] = repair_result.message
                loop_metadata["next_action"] = "rerun_preflight"

            preflight_exit_code = 0
            try:
                if options.verbose or options.watch:
                    preflight(agent=agent, project=project, verbose=options.verbose, watch=options.watch)
                else:
                    preflight(agent=agent, project=project)
            except typer.Exit as exc:
                preflight_exit_code = int(exc.exit_code or 1)

            gate_result = load_gate_result(run_dir)
            if gate_result is None:
                loop_metadata["status"] = "stopped"
                loop_metadata["last_action"] = "missing_gate_result"
                loop_metadata["stop_reason"] = "No gate result found after preflight."
                loop_metadata["next_action"] = "rerun_preflight"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            log_path = Path(str(gate_result.get("log_path", run_dir / "preflight.log")))
            log_tail = _load_log_tail(log_path)
            signature = failure_signature(gate_result, log_tail)

            loop_metadata["last_log_path"] = str(log_path)
            loop_metadata["preflight_log_paths"] = [
                *list(loop_metadata.get("preflight_log_paths", [])),
                str(log_path),
            ]
            loop_metadata["failure_signatures"] = [*list(loop_metadata.get("failure_signatures", [])), signature]

            if gate_result.get("passed"):
                updated_state = load_agent_state(project, agent)
                updated_state["state"] = AgentLifecycleState.preflight_passed.value
                save_agent_state(project, agent, updated_state)
                loop_metadata["status"] = "passed"
                loop_metadata["last_action"] = "preflight_passed"
                loop_metadata["stop_reason"] = "preflight_passed"
                loop_metadata["next_action"] = f"cascade finish {agent} --project {project}"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            classification = classify_gate_failure(log_tail)
            category = str(classification.get("category", "unknown"))
            hook = str(classification.get("hook") or "")
            dirty_file_path = str(classification.get("dirty_file_path") or "")
            loop_metadata["last_failure_category"] = category
            loop_metadata["last_failure_hook"] = hook or None
            loop_metadata["last_dirty_file_path"] = dirty_file_path or None
            loop_metadata["last_failure_suggested_action"] = str(
                classification.get("suggested_no_model_action") or ""
            )

            metadata_payload = read_mandate_metadata(
                worktree,
                str(agent_state.get("slug", "")),
            )
            if isinstance(metadata_payload, dict):
                precommit_failures = int(metadata_payload.get("precommit_failures", 0) or 0)
                if precommit_failures >= 3:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["last_action"] = "precommit_failures_limit"
                    loop_metadata["stop_reason"] = "precommit-failures-limit"
                    loop_metadata["next_action"] = "Protocol stop: 3 consecutive pre-commit failures require human intervention."
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

            if hook == "validation-slot-timeout":
                validation_slot_timeouts += 1
                if validation_slot_timeouts <= 2:
                    backoff_seconds = 30 * validation_slot_timeouts
                    loop_metadata["last_action"] = f"validation_slot_timeout_retry_{validation_slot_timeouts}"
                    loop_metadata["next_action"] = f"retry_preflight_after_{backoff_seconds}s"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    time.sleep(backoff_seconds)
                    continue
                loop_metadata["status"] = "needs_human"
                loop_metadata["last_action"] = "validation_slot_timeout_persistent"
                loop_metadata["stop_reason"] = "validation-slot-timeout-persistent"
                loop_metadata["next_action"] = "Validation slot lock timeout persisted after retries."
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata
            else:
                validation_slot_timeouts = 0

            if (
                last_deterministic_repair_kind is not None
                and last_deterministic_repair_signature is not None
                and signature == last_deterministic_repair_signature
            ):
                loop_metadata["status"] = "stopped"
                loop_metadata["last_action"] = "repeated_failure_after_repair"
                loop_metadata["stop_reason"] = "repeated_failure_after_repair"
                loop_metadata["last_repair_kind"] = last_deterministic_repair_kind
                loop_metadata["last_repair_result"] = "failed"
                loop_metadata["next_action"] = f"inspect_log:{log_path}"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            if hook == "docker-buildkit":
                loop_metadata["status"] = "needs_human"
                loop_metadata["last_action"] = "docker_buildkit_environment_failure"
                loop_metadata["stop_reason"] = "docker-buildkit"
                loop_metadata["last_repair_kind"] = "docker-buildkit"
                loop_metadata["last_repair_result"] = "failed"
                loop_metadata["next_action"] = (
                    "Set DOCKER_BUILDKIT=1 and COMPOSE_DOCKER_CLI_BUILD=1 in .env, "
                    "then rebuild the Cascade image: make rebuild"
                )
                loop_metadata["next_command"] = "make rebuild"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            if hook == "docker-runtime-network":
                if docker_runtime_network_repairs >= 1:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["last_action"] = "docker_runtime_network_persistent"
                    loop_metadata["stop_reason"] = "docker-runtime-network-persistent-after-repair"
                    loop_metadata["last_repair_kind"] = RepairKind.docker_runtime_network.value
                    loop_metadata["last_repair_result"] = "failed"
                    loop_metadata["next_action"] = "Inspect Docker Compose state for this worktree and rerun check."
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

                runtime_repair = run_repair(
                    project_config,
                    agent_state,
                    kind=RepairKind.docker_runtime_network,
                    dry_run=options.dry_run,
                    allow_stash=True,
                    active_branch_override=active_branch,
                    runtime_log_text=log_tail,
                )
                if not runtime_repair.success:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["stop_reason"] = runtime_repair.message
                    loop_metadata["last_repair_kind"] = RepairKind.docker_runtime_network.value
                    loop_metadata["last_repair_result"] = "failed"
                    loop_metadata["next_action"] = f"inspect_log:{runtime_repair.log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

                docker_runtime_network_repairs += 1
                loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                loop_metadata["last_repair_kind"] = RepairKind.docker_runtime_network.value
                loop_metadata["last_repair_result"] = "success"
                loop_metadata["last_action"] = runtime_repair.message
                loop_metadata["next_action"] = "rerun_preflight"
                last_deterministic_repair_kind = RepairKind.docker_runtime_network.value
                last_deterministic_repair_signature = signature
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                continue

            if hook == "mandate-dirty-file":
                loop_metadata["last_action"] = "closeout_dirty_file_detected"

                dirty_file_from_classification = str(classification.get("dirty_file_path", ""))
                if dirty_file_from_classification and project_config.dirty_file_repairs.auto_revert_tracked:
                    auto_revert_tracked = project_config.dirty_file_repairs.auto_revert_tracked
                    never_revert = project_config.dirty_file_repairs.never_revert
                    is_safe_revert = (
                        _file_matches_any_pattern(dirty_file_from_classification, auto_revert_tracked)
                        and not _file_matches_any_pattern(dirty_file_from_classification, never_revert)
                    )

                    if is_safe_revert:
                        dirty_file_result = repair_dirty_tracked_file(
                            project_config,
                            agent_state,
                            file_path=dirty_file_from_classification,
                            dry_run=options.dry_run,
                        )
                        if dirty_file_result.success:
                            loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                            loop_metadata["last_repair_kind"] = "dirty-file-auto-revert"
                            loop_metadata["last_repair_result"] = "success"
                            loop_metadata["last_action"] = f"Auto-reverted safe dirty file: {dirty_file_from_classification}"
                            loop_metadata["next_action"] = "rerun_preflight"
                            last_deterministic_repair_kind = "dirty-file-auto-revert"
                            last_deterministic_repair_signature = signature
                            loop_metadata["updated_at"] = timestamp_utc()
                            metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                            console.print("[loop] rerunning preflight", markup=False)
                            continue

                closeout_prep_result = prepare_mandate_closeout_dirty_files(
                    project_config,
                    agent_state,
                    stage=not options.dry_run,
                    commit=False,
                    yes=False,
                    dry_run=options.dry_run,
                    commit_message=None,
                )

                if closeout_prep_result.success:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["stop_reason"] = "dirty_file_commit_required"
                    loop_metadata["last_repair_kind"] = RepairKind.closeout_dirty_file_prep.value
                    loop_metadata["last_repair_result"] = "success"
                    loop_metadata["last_action"] = closeout_prep_result.message
                    loop_metadata["next_command"] = (
                        f"cascade closeout-prep {agent} --project {project} --stage --commit --yes"
                    )
                    loop_metadata["next_action"] = (
                        "Run closeout-prep commit, then rerun preflight."
                    )
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

                loop_metadata["status"] = "needs_human"
                loop_metadata["stop_reason"] = "dirty_file_suspicious_extras"
                loop_metadata["last_repair_kind"] = RepairKind.closeout_dirty_file_prep.value
                loop_metadata["last_repair_result"] = "failed"
                loop_metadata["last_action"] = closeout_prep_result.message
                loop_metadata["next_command"] = (
                    f"cascade closeout-prep {agent} --project {project} --stage"
                )
                loop_metadata["next_action"] = "Review suspicious extras flagged by closeout-prep before committing."
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            if hook == "mandate-metadata":
                loop_metadata["last_action"] = "mandate_metadata_detected"
                metadata_result = repair_missing_mandate_metadata(
                    project_config,
                    agent_state,
                    dry_run=options.dry_run,
                    allow_stash=True,
                    active_branch_override=active_branch,
                )
                if not metadata_result.success:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["stop_reason"] = "mandate_metadata_requires_closeout_action"
                    loop_metadata["last_repair_kind"] = "missing-mandate-metadata"
                    loop_metadata["last_repair_result"] = "failed"
                    loop_metadata["next_command"] = f"cascade diff {agent} --project {project}"
                    loop_metadata["next_action"] = "human_review"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata
                loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                loop_metadata["last_repair_kind"] = "missing-mandate-metadata"
                loop_metadata["last_repair_result"] = "success"
                loop_metadata["last_action"] = metadata_result.message
                loop_metadata["next_action"] = "rerun_preflight"
                last_deterministic_repair_kind = "missing-mandate-metadata"
                last_deterministic_repair_signature = signature
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                continue

            if hook == "missing-workspace-link":
                loop_metadata["last_action"] = "missing_workspace_link_detected"
                workspace_link_result = repair_missing_workspace_links(
                    project_config,
                    agent_state,
                    dry_run=options.dry_run,
                )
                if not workspace_link_result.success:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["stop_reason"] = workspace_link_result.message
                    loop_metadata["last_repair_kind"] = RepairKind.missing_workspace_link.value
                    loop_metadata["last_repair_result"] = "failed"
                    loop_metadata["next_command"] = (
                        f"cascade repair {agent} --project {project} --kind missing-workspace-link"
                    )
                    loop_metadata["next_action"] = f"inspect_log:{workspace_link_result.log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata
                loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                loop_metadata["last_repair_kind"] = workspace_link_result.kind.value
                loop_metadata["last_repair_result"] = "success"
                loop_metadata["last_action"] = workspace_link_result.message
                loop_metadata["next_action"] = "rerun_preflight"
                last_deterministic_repair_kind = workspace_link_result.kind.value
                last_deterministic_repair_signature = signature
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                continue

            if hook == "stale-docker-era-state":
                migration_result = run_repair(
                    project_config,
                    agent_state,
                    kind=RepairKind.docker_era_state,
                    dry_run=options.dry_run,
                    allow_stash=True,
                    active_branch_override=active_branch,
                )
                if not migration_result.success:
                    loop_metadata["status"] = "needs_human"
                    loop_metadata["stop_reason"] = migration_result.message
                    loop_metadata["last_repair_kind"] = RepairKind.docker_era_state.value
                    loop_metadata["last_repair_result"] = "failed"
                    loop_metadata["next_action"] = f"inspect_log:{migration_result.log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata
                loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                loop_metadata["last_repair_kind"] = RepairKind.docker_era_state.value
                loop_metadata["last_repair_result"] = "success"
                loop_metadata["last_action"] = migration_result.message
                loop_metadata["next_action"] = "rerun_preflight"
                last_deterministic_repair_kind = RepairKind.docker_era_state.value
                last_deterministic_repair_signature = signature
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                agent_state = load_agent_state(project, agent)
                continue

            if signature == last_signature:
                repeat_count += 1
            else:
                repeat_count = 1
                last_signature = signature

            if stop_on_same_failure_twice and repeat_count >= 2:
                loop_metadata["status"] = "stopped"
                loop_metadata["last_action"] = "same_failure_repeated"
                loop_metadata["stop_reason"] = "Same failure repeated; stopping to avoid wasting model budget."
                loop_metadata["next_action"] = f"inspect_log:{log_path}"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            if category in approval_categories:
                loop_metadata["status"] = "needs_human"
                loop_metadata["last_action"] = "approval_required"
                loop_metadata["stop_reason"] = f"Category '{category}' requires human approval."
                loop_metadata["next_action"] = "human_approval"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            touched_before = get_touched_files(worktree)
            deterministic_ran = False
            deterministic_message = ""

            rule = _resolve_routing_rule(project_config, category)
            strategy = str(rule.get("strategy", "diagnose_only"))
            if strategy == "deterministic_first":
                deterministic_ran, deterministic_message = _run_configured_gate_fix(
                    project_config,
                    hook,
                    worktree=worktree,
                    dry_run=options.dry_run,
                )
                if not deterministic_ran and not bool(classification.get("model_recommended", True)):
                    loop_metadata["status"] = "stopped"
                    loop_metadata["last_action"] = "deterministic_fix_not_configured"
                    loop_metadata["stop_reason"] = deterministic_message
                    loop_metadata["next_action"] = f"inspect_log:{log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

            if not deterministic_ran and not bool(classification.get("model_recommended", True)):
                loop_metadata["status"] = "stopped"
                if strategy == "deterministic_first":
                    loop_metadata["last_action"] = "deterministic_fix_not_configured"
                    loop_metadata["stop_reason"] = deterministic_message or "No configured deterministic autofix command for this failure."
                else:
                    loop_metadata["last_action"] = "preflight_failed_unhandled"
                    loop_metadata["stop_reason"] = "preflight_failed_unhandled"
                    loop_metadata["next_command"] = f"cascade gate-summary {agent} --project {project}"
                    loop_metadata["next_action"] = "gate_summary"
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            if not deterministic_ran and not options.dry_run:
                if int(loop_metadata.get("model_fix_attempts", 0)) >= max_model_fixes:
                    loop_metadata["status"] = "stopped"
                    loop_metadata["last_action"] = "max_model_fixes_reached"
                    loop_metadata["stop_reason"] = "Maximum model fix attempts reached."
                    loop_metadata["next_action"] = f"inspect_log:{log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

                estimated_spent = float(loop_metadata.get("estimated_cost_spent", 0.0))
                if loop_non_interactive is None:
                    loop_non_interactive, mode_warning = _resolve_loop_non_interactive_mode(options.non_interactive)
                    if mode_warning and not loop_mode_warning_printed:
                        print_warning(mode_warning)
                        loop_mode_warning_printed = True

                branch_before_model = get_current_branch(worktree)

                model_ok, model_message, estimated_cost, profile_used = _run_model_fix_attempt(
                    project=project,
                    agent=agent,
                    project_config=project_config,
                    agent_state=agent_state,
                    options=options,
                    run_dir=run_dir,
                    iteration=iteration,
                    classification=classification,
                    log_tail=log_tail,
                    gate_result=gate_result,
                    non_interactive=bool(loop_non_interactive),
                )
                branch_after_model = get_current_branch(worktree)
                if branch_before_model != branch_after_model:
                    loop_metadata["status"] = "stopped"
                    loop_metadata["last_action"] = "model_branch_switch_detected"
                    loop_metadata["stop_reason"] = "model-branch-switch"
                    loop_metadata["next_action"] = (
                        f"Model changed branch from '{branch_before_model}' to '{branch_after_model}'. "
                        "Stop and restore expected agent branch before continuing."
                    )
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

                branch_error_after_model = get_agent_branch_mismatch_error(
                    project_config,
                    worktree=worktree,
                    agent=agent,
                    slug=str(agent_state.get("slug", "")),
                )
                if branch_error_after_model is not None:
                    loop_metadata["status"] = "stopped"
                    loop_metadata["last_action"] = "model_branch_violation"
                    loop_metadata["stop_reason"] = "mandate-agent-branch-mismatch"
                    loop_metadata["next_action"] = branch_error_after_model
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata
                if estimated_spent + estimated_cost > max_budget:
                    loop_metadata["status"] = "stopped"
                    loop_metadata["last_action"] = "budget_exceeded"
                    loop_metadata["stop_reason"] = "Estimated model budget exceeded."
                    loop_metadata["next_action"] = f"inspect_log:{log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata

                loop_metadata["estimated_cost_spent"] = round(estimated_spent + estimated_cost, 6)
                loop_metadata["estimated_cost_used"] = loop_metadata["estimated_cost_spent"]
                loop_metadata["model_fix_attempts"] = int(loop_metadata.get("model_fix_attempts", 0)) + 1
                loop_metadata["model_fixes_used"] = loop_metadata["model_fix_attempts"]
                if profile_used is not None:
                    loop_metadata["profiles_used"] = [*list(loop_metadata.get("profiles_used", [])), profile_used]
                    loop_metadata["model_profiles_used"] = loop_metadata["profiles_used"]
                loop_metadata["last_action"] = model_message
                if not model_ok:
                    loop_metadata["status"] = "stopped"
                    if model_message == "open_code_exit_nonzero":
                        loop_metadata["stop_reason"] = "open_code_exit_nonzero"
                        opencode_log_path = run_dir / f"loop_opencode_{iteration}.log"
                        loop_metadata["next_action"] = f"inspect_log:{opencode_log_path}"
                    else:
                        loop_metadata["stop_reason"] = model_message
                        loop_metadata["next_action"] = f"inspect_log:{log_path}"
                    loop_metadata["updated_at"] = timestamp_utc()
                    metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                    return loop_metadata
                console.print("[loop] rerunning preflight", markup=False)
            else:
                loop_metadata["deterministic_repairs_used"] = int(loop_metadata.get("deterministic_repairs_used", 0)) + 1
                loop_metadata["last_repair_kind"] = str(classification.get("repair_kind") or hook or "configured-gate-fix")
                loop_metadata["last_repair_result"] = "success"
                loop_metadata["last_action"] = deterministic_message or "deterministic_fix_skipped"
                loop_metadata["next_action"] = "rerun_preflight"
                last_deterministic_repair_kind = str(classification.get("repair_kind") or hook or "configured-gate-fix")
                last_deterministic_repair_signature = signature

            if not deterministic_ran:
                last_deterministic_repair_kind = None
                last_deterministic_repair_signature = None

            touched_after = get_touched_files(worktree)
            forbidden = _forbidden_touched_files(
                touched_after,
                project_config.repair_loop.forbidden_touched_file_patterns,
            )
            if forbidden:
                loop_metadata["status"] = "needs_human"
                loop_metadata["last_action"] = "forbidden_files_touched"
                loop_metadata["stop_reason"] = f"Forbidden files touched during fix attempt: {', '.join(forbidden)}"
                loop_metadata["next_action"] = "human_review"
                loop_metadata["touched_before_fix"] = touched_before
                loop_metadata["touched_after_fix"] = touched_after
                loop_metadata["updated_at"] = timestamp_utc()
                metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
                return loop_metadata

            loop_metadata["touched_before_fix"] = touched_before
            loop_metadata["touched_after_fix"] = touched_after
            loop_metadata["updated_at"] = timestamp_utc()
            metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")

            if preflight_exit_code == 0 and gate_result.get("passed"):
                break

        if loop_metadata.get("status") == "running":
            loop_metadata["status"] = "stopped"
            loop_metadata["last_action"] = "max_iterations_reached"
            loop_metadata["stop_reason"] = "Maximum loop iterations reached."
            loop_metadata["next_action"] = f"inspect_log:{loop_metadata.get('last_log_path', '')}"
            loop_metadata["updated_at"] = timestamp_utc()
            metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        loop_metadata["status"] = "failed"
        loop_metadata["last_action"] = "loop_exception"
        loop_metadata["stop_reason"] = str(exc)
        loop_metadata["next_action"] = "inspect_exception"
        loop_metadata["updated_at"] = timestamp_utc()
        metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")
        raise

    if loop_metadata.get("status") == "running":
        loop_metadata["status"] = "stopped"
        loop_metadata["last_action"] = "loop_returned_without_terminal_state"
        loop_metadata["stop_reason"] = "Loop ended without terminal state."
        loop_metadata["next_action"] = f"inspect_log:{loop_metadata.get('last_log_path', '')}"
        loop_metadata["updated_at"] = timestamp_utc()
        metadata_path.write_text(json.dumps(loop_metadata, indent=2) + "\n", encoding="utf-8")

    return loop_metadata


@app.command(help="Run bounded auto-repair loop: preflight, classify, fix, and re-run until pass or limits.")
def loop(
    agent: str,
    project: str = typer.Option(...),
    max_iterations: int | None = typer.Option(None, "--max-iterations"),
    max_model_fixes: int | None = typer.Option(None, "--max-model-fixes"),
    max_estimated_cost: float | None = typer.Option(None, "--max-estimated-cost"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose"),
    watch: bool = typer.Option(False, "--watch"),
    non_interactive: bool | None = typer.Option(
        None,
        "--non-interactive/--interactive",
        help="Use non-interactive OpenCode runs for loop model-fix attempts (default: auto-detect and prefer non-interactive).",
    ),
    profile: str | None = typer.Option(None, "--profile"),
    cheap_profile: str | None = typer.Option(None, "--cheap-profile"),
    debug_profile: str | None = typer.Option(None, "--debug-profile"),
    executor_profile: str | None = typer.Option(None, "--executor-profile"),
    active_branch: str | None = typer.Option(None, "--active-branch"),
    finish_on_pass: bool = typer.Option(False, "--finish-on-pass"),
) -> None:
    if watch and non_interactive is False:
        print_error("--watch requires non-interactive mode. Remove --interactive or use --non-interactive.")
        raise typer.Exit(1)

    options = _resolve_loop_options(
        max_iterations,
        max_model_fixes,
        max_estimated_cost,
        dry_run,
        non_interactive,
        verbose,
        watch,
        profile,
        cheap_profile,
        debug_profile,
        executor_profile,
    )
    try:
        result = run_auto_repair_loop(
            project=project,
            agent=agent,
            options=options,
            active_branch=active_branch,
        )
    except (FileNotFoundError, ValueError, ConfigError, CommandError, OpenCodeError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    console.print(f"Repair loop metadata: {_repair_loop_metadata_path(project, agent)}")
    console.print(f"Loop status: {result.get('status')}")
    console.print(f"Iterations: {result.get('iterations')}")
    console.print(f"Model fixes: {result.get('model_fix_attempts')}")
    console.print(f"Estimated spend: ${float(result.get('estimated_cost_spent', 0.0)):.4f}")
    if result.get("status") == "passed":
        console.print("[green]Loop complete: preflight passed.[/green]")
        if finish_on_pass:
            finish(agent=agent, project=project, dry_run=False, yes=True)
        else:
            console.print(f"Next: cascade finish {agent} --project {project}")
        return
    if result.get("status") != "passed":
        stop_reason = str(result.get("stop_reason") or "Repair loop stopped.")
        print_warning(stop_reason)
        last_log_path = result.get("last_log_path")
        if isinstance(last_log_path, str) and last_log_path:
            console.print(f"Log: {last_log_path}")
        next_command = result.get("next_command")
        if isinstance(next_command, str) and next_command:
            console.print(f"Next: {next_command}")
        dirty_file_path = result.get("last_dirty_file_path")
        if isinstance(dirty_file_path, str) and dirty_file_path:
            console.print(f"Dirty file: {dirty_file_path}")
        raise typer.Exit(1)


@app.command(name="loop-status", help="Print the latest auto-repair loop status metadata for an agent.")
def loop_status(agent: str, project: str = typer.Option(...)) -> None:
    metadata_path = _repair_loop_metadata_path(project, agent)
    if not metadata_path.exists():
        print_error(f"No loop metadata found at {metadata_path}. Run `cascade loop` first.")
        raise typer.Exit(1)

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    status_value = str(payload.get("status", "(unknown)"))
    if status_value == "running":
        payload["status"] = "stopped"
        if payload.get("stop_reason") in (None, "", "(none)"):
            payload["stop_reason"] = "loop_process_not_active"
        if payload.get("last_action") in (None, "", "run_preflight"):
            payload["last_action"] = "loop_process_not_active"
        metadata_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    table = Table(title=f"Loop Status: {project}/{agent}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Status", str(payload.get("status", "(unknown)")))
    table.add_row("Stop reason", str(payload.get("stop_reason", "(none)")))
    table.add_row("Iterations", str(payload.get("iterations", 0)))
    table.add_row("Model fixes", str(payload.get("model_fixes_used", payload.get("model_fix_attempts", 0))))
    table.add_row("Estimated spend", f"${float(payload.get('estimated_cost_used', payload.get('estimated_cost_spent', 0.0))):.4f}")
    table.add_row("Last failure", str(payload.get("last_failure_category", "(none)")))
    table.add_row("Last hook", str(payload.get("last_failure_hook", "(none)")))
    table.add_row("Dirty file", str(payload.get("last_dirty_file_path", "(none)")))
    table.add_row("Last action", str(payload.get("last_action", "(none)")))
    next_cmd = str(payload.get("next_command") or "")
    if not next_cmd:
        next_cmd = (
            f"cascade finish {agent} --project {project}"
            if str(payload.get("status")) == "passed"
            else f"cascade check {agent} --project {project}"
        )
    table.add_row("Next", next_cmd)
    console.print(table)


@app.command(help="Prepare fix context from the latest gate failure and optionally launch OpenCode.")
def fix(
    agent: str,
    project: str = typer.Option(...),
    profile: str = typer.Option(...),
    no_launch: bool = typer.Option(False, "--no-launch"),
    force_model: bool = typer.Option(False, "--force-model"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    log_path = run_dir / "preflight.log"
    try:
        log_text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        print_error(f"No preflight log found at {log_path}. Run `cascade check` first.")
        raise typer.Exit(1) from exc

    gate_summary(agent=agent, project=project)
    classification = classify_gate_failure(log_text)
    if not classification.get("model_recommended") and not force_model:
        console.print(
            f"Deterministic suggested action: {classification.get('suggested_no_model_action', 'Inspect the gate log manually.')}"
        )
        console.print("Model launch skipped because the failure looks deterministic. Use --force-model to override.")
        return

    project_file_value = agent_state.get("project_file")
    if not isinstance(project_file_value, str) or not project_file_value:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(Path(project_file_value))
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)

    prepare_model_call(
        agent=agent,
        project=project,
        task="fix",
        profile=profile,
        include_diff=False,
    )

    prompt_path = run_dir / "fix_prompt.md"
    meta_path = run_dir / "fix_model_call.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    console.print(f"Prompt file   : {prompt_path}")
    console.print(f"Profile       : {profile}")
    console.print(f"Model         : {metadata.get('model_id', '(unknown)')}")
    console.print(f"Est. cost USD : {metadata.get('estimated_cost_usd', '(unknown)')}")

    if no_launch:
        console.print("[green]Fix context prepared (no launch).[/green]")
        console.print(f"Next: opencode . --model {metadata.get('model_id', '(model from metadata)')}")
        return

    try:
        ensure_opencode_available()
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    console.print("Use the fix prompt file above. Fix only the specific failure; no unrelated refactors, no gate weakening, no commit/push.")
    run_agent(agent=agent, project=project, task="fix")


@app.command(help="Verify closeout readiness and optionally mark the agent closeout_ready.")
def finish(
    agent: str,
    project: str = typer.Option(...),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_config = load_project_from_agent_state(agent_state)
    if project_config is None:
        print_error("Unable to load project config from agent state.")
        raise typer.Exit(1)

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    is_valid_location, location_message = validate_worktree_location(project_config, worktree)
    if not is_valid_location:
        print_error(location_message)
        raise typer.Exit(1)

    if project_config.branches.agent_branch_template is not None:
        branch_warning = validate_agent_branch(project_config, agent_state, worktree)
        if branch_warning is not None:
            print_error(branch_warning)
            raise typer.Exit(1)

    run_dir = get_agent_run_dir(project, agent)
    gate_result = load_gate_result(run_dir)
    if gate_result is None:
        print_error("No gate result found. Run `cascade check` first.")
        raise typer.Exit(1)
    if not gate_result.get("passed"):
        print_error("Latest preflight did not pass. Run `cascade check` or `cascade fix` first.")
        raise typer.Exit(1)

    is_stale, stale_reason = check_gate_staleness(gate_result, worktree)
    if is_stale:
        print_error(f"Latest preflight is stale: {stale_reason}")
        raise typer.Exit(1)

    summary_path = run_dir / "closeout_summary.md"
    mandate_id = read_mandate_id(worktree, str(agent_state.get("slug", "")))
    summary_body = (
        "# Closeout Summary\n\n"
        f"- Project: {project}\n"
        f"- Agent: {agent}\n"
        f"- Issue: #{agent_state.get('issue', '')}\n"
        f"- Title: {agent_state.get('title', '')}\n"
        f"- Worktree: {worktree}\n"
        f"- Branch: {get_current_branch(worktree)}\n"
        f"- Mandate ID: {mandate_id or '(missing)'}\n"
        f"- Gate: {gate_status_line(gate_result, worktree)}\n"
        f"- Git status: {get_git_status(worktree) or '(clean)'}\n"
        f"- Diff stat: {get_git_diff_stat(worktree) or '(none)'}\n"
        f"- Changed files: {get_git_diff_names(worktree) or '(none)'}\n"
        "\nSafety: no push or cleanup has been performed.\n"
    )
    summary_path.write_text(summary_body, encoding="utf-8")
    console.print(f"Closeout summary: {summary_path}")

    if not yes:
        console.print("[yellow]Dry run only.[/yellow] Re-run with --yes to mark closeout_ready.")
        return

    mark(agent=agent, project=project, state=AgentLifecycleState.closeout_ready)


@app.command(help="Execute configured closeout command and transition mandate lifecycle to closed.")
def closeout(
    agent: str,
    project: str = typer.Option(...),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    if not yes:
        print_error("Closeout is destructive. Re-run with --yes to execute configured closeout command.")
        raise typer.Exit(1)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_config = load_project_from_agent_state(agent_state)
    if project_config is None:
        print_error("Unable to load project config from agent state.")
        raise typer.Exit(1)

    if project_config.commands.done is None:
        print_error("Project config does not define commands.done required for closeout.")
        raise typer.Exit(1)

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    state_value = str(agent_state.get("state") or "")
    if state_value not in {
        AgentLifecycleState.closeout_ready.value,
        AgentLifecycleState.preflight_passed.value,
    }:
        print_error(
            "Closeout requires closeout_ready or preflight_passed lifecycle state. "
            f"Current state: {state_value or '(missing)'}"
        )
        raise typer.Exit(1)

    mismatch = get_agent_branch_mismatch_error(
        project_config,
        worktree=worktree,
        agent=agent,
        slug=str(agent_state.get("slug", "")),
    )
    if mismatch is not None:
        print_error(mismatch)
        raise typer.Exit(1)

    run_dir = get_agent_run_dir(project, agent)
    gate_result = load_gate_result(run_dir)
    if gate_result is None or not gate_result.get("passed"):
        print_error("Closeout requires a passing gate result. Run `cascade check` first.")
        raise typer.Exit(1)

    is_stale, stale_reason = check_gate_staleness(gate_result, worktree)
    if is_stale:
        print_error(f"Closeout blocked because gate result is stale: {stale_reason}")
        raise typer.Exit(1)

    updated = dict(agent_state)
    updated["state"] = AgentLifecycleState.closing_out.value
    updated["closeout_started_at"] = timestamp_utc()
    save_agent_state(project, agent, updated)

    done_command = format_command_template(
        project_config.commands.done,
        project=project_config,
        agent=agent,
        slug=str(agent_state.get("slug", "")),
        issue=int(agent_state.get("issue", 0) or 0),
        title=str(agent_state.get("title", "")),
        canonical_mandate=run_dir / "mandate.md",
    )

    try:
        run_command(done_command, cwd=worktree)
    except CommandError as exc:
        classification = classify_gate_failure(str(exc.output))
        if str(classification.get("hook") or "") == "docker-runtime-network":
            repair_result = run_repair(
                project_config,
                agent_state,
                kind=RepairKind.docker_runtime_network,
                dry_run=False,
                allow_stash=True,
                active_branch_override=None,
                runtime_log_text=str(exc.output),
            )
            console.print(f"Repair log: {repair_result.log_path}")
            if repair_result.success:
                print_warning("Deterministic docker-runtime-network repair applied; retrying closeout command once.")
                try:
                    run_command(done_command, cwd=worktree)
                except CommandError as retry_exc:
                    failed = load_agent_state(project, agent)
                    failed["state"] = AgentLifecycleState.closeout_failed.value
                    failed["closeout_failed_at"] = timestamp_utc()
                    save_agent_state(project, agent, failed)
                    print_error(
                        "docker-runtime-network persisted after deterministic closeout retry.\n"
                        f"{retry_exc}"
                    )
                    raise typer.Exit(1) from retry_exc
            else:
                failed = load_agent_state(project, agent)
                failed["state"] = AgentLifecycleState.closeout_failed.value
                failed["closeout_failed_at"] = timestamp_utc()
                save_agent_state(project, agent, failed)
                print_error(f"Deterministic docker-runtime-network repair failed: {repair_result.message}")
                raise typer.Exit(1) from exc
        else:
            closeout_failure_log = run_dir / "closeout_failure.log"
            closeout_failure_log.write_text(str(exc.output), encoding="utf-8")
            closeout_failure_context = {
                "source": "closeout",
                "timestamp": timestamp_utc(),
                "command": done_command,
                "hook": str(classification.get("hook") or "unknown"),
                "log": str(exc.output),
                "log_path": str(closeout_failure_log),
                "touched_files": get_touched_files(worktree),
            }
            _save_failure_context(run_dir, _CLOSEOUT_FAILURE_CONTEXT_FILENAME, closeout_failure_context)
            _save_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME, closeout_failure_context)
            failed = load_agent_state(project, agent)
            failed["state"] = AgentLifecycleState.closeout_failed.value
            failed["closeout_failed_at"] = timestamp_utc()
            save_agent_state(project, agent, failed)
            print_error(str(exc))
            raise typer.Exit(1) from exc

    closed = load_agent_state(project, agent)
    closed["state"] = AgentLifecycleState.closed.value
    closed["closeout_completed_at"] = timestamp_utc()
    closed["squash_commit_sha"] = get_git_head_sha(worktree)
    save_agent_state(project, agent, closed)

    project_meta = read_github_project_config(worktree)
    if project_meta is not None:
        try:
            project_number = int(project_meta.get("project_number", 0) or 0)
        except (TypeError, ValueError):
            project_number = 0
        if project_number > 0:
            linked = get_project_item_for_issue(
                owner=project_config.github.owner,
                repo=project_config.github.repo,
                project_number=project_number,
                issue_number=int(agent_state.get("issue", 0) or 0),
            )
            if linked is not None:
                item_id = str(linked.get("item_id") or "")
                project_id = str(linked.get("project_id") or "")
                status_field_id = str(project_meta.get("status_field_id") or "")
                done_option_id = str(project_meta.get("done_option_id") or "")
                if item_id and project_id and status_field_id and done_option_id:
                    done_ok = update_project_v2_item_status(
                        project_id=project_id,
                        item_id=item_id,
                        field_id=status_field_id,
                        option_id=done_option_id,
                    )
                    if not done_ok:
                        print_warning("GitHub Project status sync failed (done).")

    if project_config.commands.propagate:
        propagate_command = format_command_template(
            project_config.commands.propagate,
            project=project_config,
            agent=agent,
            slug=str(agent_state.get("slug", "")),
            issue=int(agent_state.get("issue", 0) or 0),
            title=str(agent_state.get("title", "")),
            canonical_mandate=run_dir / "mandate.md",
        )
        try:
            run_command(propagate_command, cwd=worktree)
        except CommandError as exc:
            print_warning(f"Post-closeout propagate failed (non-blocking): {exc}")

    _clear_failure_context(run_dir, _CLOSEOUT_FAILURE_CONTEXT_FILENAME)
    _clear_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME)

    console.print("[green]Closeout completed successfully.[/green]")


@app.command(help="Run mandate completion end-to-end: preflight repair, commit, post-commit preflight, finish, and closeout.")
def complete(
    agent: str,
    project: str = typer.Option(...),
    yes: bool = typer.Option(False, "--yes"),
    active_branch: str | None = typer.Option(None, "--active-branch"),
    verbose: bool = typer.Option(False, "--verbose"),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    if not yes:
        print_error("Complete is destructive. Re-run with --yes to allow commit and closeout.")
        raise typer.Exit(1)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(project_file_path)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    emit_standards_warnings(project_config, agent_state, worktree)

    run_dir = get_agent_run_dir(project, agent)
    metadata_path = _complete_loop_metadata_path(project, agent)
    existing_metadata = _load_json_artifact(metadata_path)
    start_phase = _resolve_complete_start_phase(agent_state=agent_state, existing_metadata=existing_metadata)
    if start_phase is None:
        payload = _initialize_complete_metadata(
            project=project,
            agent=agent,
            agent_state=agent_state,
            resume_from="completed",
            existing_metadata=existing_metadata,
        )
        payload["status"] = "completed"
        payload["current_phase"] = "completed"
        payload["next_phase"] = None
        payload["final_agent_state"] = str(agent_state.get("state") or "")
        _write_json_artifact(metadata_path, payload)
        console.print(f"Complete loop artifact: {metadata_path}")
        console.print("[green]Mandate is already complete.[/green]")
        return

    payload = _initialize_complete_metadata(
        project=project,
        agent=agent,
        agent_state=agent_state,
        resume_from=start_phase,
        existing_metadata=existing_metadata,
    )
    _write_json_artifact(metadata_path, payload)

    complete_rounds = int(payload.get("commit_rounds") or 0)
    current_phase = start_phase
    console.print(f"Complete loop artifact: {metadata_path}")

    if current_phase == "preflight" and _worktree_has_dirty_changes(worktree):
        initial_prep = prepare_mandate_closeout_dirty_files(
            project_config,
            agent_state,
            stage=False,
            commit=False,
            yes=False,
            dry_run=False,
            commit_message=None,
        )
        closeout_logs = payload.get("closeout_prep_logs")
        if not isinstance(closeout_logs, list):
            closeout_logs = []
        closeout_logs.append(str(initial_prep.log_path))
        payload["closeout_prep_logs"] = closeout_logs
        payload["initial_closeout_prep_message"] = initial_prep.message
        if not initial_prep.success:
            _set_complete_metadata(
                metadata_path,
                payload,
                status="needs_human",
                current_phase="closeout_prep_stage",
                next_phase="closeout_prep_stage",
                stop_reason=initial_prep.message,
            )
            print_error(initial_prep.message)
            raise typer.Exit(1)
        current_phase = "closeout_prep_stage"
        _set_complete_metadata(
            metadata_path,
            payload,
            current_phase=current_phase,
            next_phase=current_phase,
            note="dirty mandate-owned files detected; switching to closeout prep stage",
        )

    try:
        while current_phase in _DEFAULT_COMPLETE_PHASES:
            _set_complete_metadata(
                metadata_path,
                payload,
                status="running",
                current_phase=current_phase,
                next_phase=current_phase,
            )

            if current_phase in {"preflight", "post_commit_preflight"}:
                loop_options = _resolve_loop_options(2, 1, 1.0, False, None, verbose, watch, None, None, None, None)
                loop_result = run_auto_repair_loop(
                    project=project,
                    agent=agent,
                    options=loop_options,
                    active_branch=active_branch,
                )
                payload["repair_loop_path"] = str(_repair_loop_metadata_path(project, agent))
                payload["last_loop_status"] = str(loop_result.get("status") or "")
                payload["last_loop_stop_reason"] = str(loop_result.get("stop_reason") or "")
                payload["last_loop_action"] = str(loop_result.get("last_action") or "")

                if str(loop_result.get("status") or "") != "passed":
                    if _is_dirty_file_commit_required_failure(loop_result):
                        payload["last_transition_reason"] = "mandate_commit_required"
                        payload["mandate_commit_required_source"] = current_phase
                        payload["mandate_commit_required_stop_reason"] = str(loop_result.get("stop_reason") or "")
                        current_phase = "closeout_prep_stage"
                        _set_complete_metadata(
                            metadata_path,
                            payload,
                            status="running",
                            current_phase=current_phase,
                            next_phase=current_phase,
                            note="preflight reported dirty-file commit required; routing to closeout prep",
                        )
                        continue

                    failure_status = str(loop_result.get("status") or "failed")
                    if failure_status not in {"needs_human", "failed"}:
                        failure_status = "failed"
                    stop_reason = str(loop_result.get("stop_reason") or "complete_preflight_failed")
                    _set_complete_metadata(
                        metadata_path,
                        payload,
                        status=failure_status,
                        current_phase=current_phase,
                        next_phase=current_phase,
                        stop_reason=stop_reason,
                    )
                    print_error(stop_reason)
                    raise typer.Exit(1)

                _append_completed_phase(payload, current_phase)
                if current_phase == "preflight":
                    current_phase = "mandate_commit"
                else:
                    current_phase = "mandate_commit" if _worktree_has_dirty_changes(worktree) else "finish"
                _set_complete_metadata(
                    metadata_path,
                    payload,
                    current_phase=current_phase,
                    next_phase=current_phase,
                    note=f"{payload['completed_phases'][-1]} passed",
                )
                continue

            if current_phase == "closeout_prep_stage":
                stage_result = prepare_mandate_closeout_dirty_files(
                    project_config,
                    agent_state,
                    stage=True,
                    commit=False,
                    yes=False,
                    dry_run=False,
                    commit_message=None,
                )
                closeout_logs = payload.get("closeout_prep_logs")
                if not isinstance(closeout_logs, list):
                    closeout_logs = []
                closeout_logs.append(str(stage_result.log_path))
                payload["closeout_prep_logs"] = closeout_logs
                payload["last_closeout_prep_message"] = stage_result.message
                if not stage_result.success:
                    _set_complete_metadata(
                        metadata_path,
                        payload,
                        status="needs_human",
                        current_phase=current_phase,
                        next_phase=current_phase,
                        stop_reason=stage_result.message,
                    )
                    print_error(stage_result.message)
                    raise typer.Exit(1)

                _append_completed_phase(payload, current_phase)
                current_phase = "mandate_commit"
                _set_complete_metadata(
                    metadata_path,
                    payload,
                    current_phase=current_phase,
                    next_phase=current_phase,
                    note="closeout prep staging completed",
                )
                continue

            if current_phase == "mandate_commit":
                complete_rounds += 1
                payload["commit_rounds"] = complete_rounds
                try:
                    result, retried = _execute_closeout_prep_flow(
                        agent=agent,
                        project=project,
                        project_config=project_config,
                        agent_state=agent_state,
                        stage=True,
                        commit=True,
                        auto_fix_gates=True,
                        yes=True,
                        dry_run=False,
                        message=None,
                    )
                except typer.Exit as exc:
                    _set_complete_metadata(
                        metadata_path,
                        payload,
                        status="needs_human",
                        current_phase=current_phase,
                        next_phase=current_phase,
                        stop_reason=f"commit auto-fix exited with code {int(exc.exit_code or 1)}",
                    )
                    raise
                closeout_logs = payload.get("closeout_prep_logs")
                if not isinstance(closeout_logs, list):
                    closeout_logs = []
                closeout_logs.append(str(result.log_path))
                payload["closeout_prep_logs"] = closeout_logs
                payload["last_closeout_prep_retried"] = retried
                payload["last_closeout_prep_message"] = result.message
                if not result.success:
                    _set_complete_metadata(
                        metadata_path,
                        payload,
                        status="needs_human",
                        current_phase=current_phase,
                        next_phase=current_phase,
                        stop_reason=result.message,
                    )
                    print_error(result.message)
                    raise typer.Exit(1)

                _append_completed_phase(payload, current_phase)
                current_phase = "post_commit_preflight"
                _set_complete_metadata(
                    metadata_path,
                    payload,
                    current_phase=current_phase,
                    next_phase=current_phase,
                    note="commit completed",
                )
                continue

            if current_phase == "finish":
                try:
                    finish(agent=agent, project=project, dry_run=False, yes=True)
                except typer.Exit as exc:
                    _set_complete_metadata(
                        metadata_path,
                        payload,
                        status="failed",
                        current_phase=current_phase,
                        next_phase=current_phase,
                        stop_reason=f"finish failed (exit={int(exc.exit_code or 1)})",
                    )
                    raise
                _append_completed_phase(payload, current_phase)
                current_phase = "closeout"
                _set_complete_metadata(
                    metadata_path,
                    payload,
                    current_phase=current_phase,
                    next_phase=current_phase,
                    note="finish completed",
                )
                continue

            if current_phase == "closeout":
                try:
                    closeout(agent=agent, project=project, yes=True)
                except typer.Exit as exc:
                    _set_complete_metadata(
                        metadata_path,
                        payload,
                        status="failed",
                        current_phase=current_phase,
                        next_phase=current_phase,
                        stop_reason=f"closeout failed (exit={int(exc.exit_code or 1)})",
                    )
                    raise
                _append_completed_phase(payload, current_phase)
                payload["final_agent_state"] = str(load_agent_state(project, agent).get("state") or "")
                _set_complete_metadata(
                    metadata_path,
                    payload,
                    status="completed",
                    current_phase="completed",
                    next_phase=None,
                    note="closeout completed",
                )
                console.print("[green]Complete flow finished successfully.[/green]")
                return
    except typer.Exit:
        raise
    except Exception as exc:
        _set_complete_metadata(
            metadata_path,
            payload,
            status="failed",
            current_phase=current_phase,
            next_phase=current_phase,
            stop_reason=str(exc),
        )
        raise

    _set_complete_metadata(
        metadata_path,
        payload,
        status="failed",
        current_phase=str(current_phase),
        next_phase=str(current_phase),
        stop_reason="complete reached unexpected phase state",
    )
    print_error("Complete reached unexpected phase state.")
    raise typer.Exit(1)


@app.command(
    name="complete-v2",
    help="Run mandate completion via the v2 lifecycle runner (preflight repair, commit, post-commit preflight, finish, and closeout).",
)
def complete_v2(
    agent: str,
    project: str = typer.Option(...),
    yes: bool = typer.Option(False, "--yes"),
    active_branch: str | None = typer.Option(None, "--active-branch"),
    verbose: bool = typer.Option(False, "--verbose"),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    complete(
        agent=agent,
        project=project,
        yes=yes,
        active_branch=active_branch,
        verbose=verbose,
        watch=watch,
    )


@app.command(help="Recommend the next high-level command based on current agent state.")
def next(agent: str, project: str = typer.Option(...)) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    state_value = str(agent_state.get("state", ""))
    run_dir = get_agent_run_dir(project, agent)
    gate_result = load_gate_result(run_dir)

    if state_value == AgentLifecycleState.blocked.value:
        console.print(f"Next: inspect logs with `cascade logs {agent} --project {project} --kind preflight`")
        console.print(f"Then review context with `cascade context {agent} --project {project} --print`")
        return

    if gate_result is None:
        console.print(f"Next: cascade check {agent} --project {project}")
        return

    if not gate_result.get("passed"):
        console.print(f"Next: cascade fix {agent} --project {project} --profile debugger")
        return

    console.print(f"Next: cascade finish {agent} --project {project}")


@app.command(name="run-agent", help="Launch interactive OpenCode session for this agent.")
def run_agent(
    agent: str,
    project: str = typer.Option(...),
    print_prompt: bool = typer.Option(False, "--print-prompt"),
    with_prompt: bool = typer.Option(True, "--with-prompt/--no-prompt"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    copy_prompt: bool = typer.Option(False, "--copy-prompt"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    task: str | None = typer.Option(None, "--task"),
    mode: OpenCodeMode | None = typer.Option(None, "--mode"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_config = load_project_from_agent_state(agent_state)
    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)
    model = str(agent_state["model"])
    run_dir = get_agent_run_dir(project, agent)

    prompt_path: Path | None = None
    prompt_text: str | None = None
    if with_prompt:
        try:
            prompt_path = resolve_prompt_path(run_dir, task=task, prompt_file=prompt_file)
            prompt_text = prompt_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            print_error(str(exc))
            raise typer.Exit(1) from exc

    if non_interactive and not with_prompt:
        print_error("--non-interactive requires prompt injection. Remove --no-prompt or provide a prompt source.")
        raise typer.Exit(1)

    console.print(f"Worktree: {worktree}")
    console.print(f"Model: {model}")
    if prompt_path is not None:
        console.print(f"Prompt: {prompt_path}")
        if platform.system() == "Darwin":
            console.print(f"Clipboard fallback: {build_prompt_copy_command(agent, project, task=task, prompt_file=prompt_file)}")
    if copy_prompt and prompt_path is not None:
        console.print(f"Copy prompt on host with: {build_prompt_copy_command(agent, project, task=task, prompt_file=prompt_file)}")
    if with_prompt and not non_interactive and prompt_path is not None:
        console.print("OpenCode will start with the selected prompt loaded automatically.")
    elif prompt_path is not None:
        console.print(f"Prompt available at: {prompt_path}")

    if print_prompt and prompt_text is not None:
        console.print(prompt_text)

    try:
        ensure_opencode_available()
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1)

    if non_interactive:
        try:
            output = run_prompt(
                prompt=prompt_text or "",
                worktree=worktree,
                model=model,
                mode=mode,
                use_continue=False,
            )
        except OpenCodeError as exc:
            print_error(str(exc))
            raise typer.Exit(1) from exc
        console.print(output)
    else:
        result = subprocess.run(
            build_interactive_command(model, mode=mode, prompt=prompt_text if with_prompt else None),
            cwd=worktree,
            check=False,
        )
        if result.returncode != 0:
            print_error(f"OpenCode exited with status {result.returncode}.")
            raise typer.Exit(result.returncode)

    agent_state["last_mode"] = mode.value if mode is not None else None
    agent_state["last_interaction_at"] = timestamp_utc()
    save_agent_state(project, agent, agent_state)


@app.command()
def status(project: str = typer.Option(...)) -> None:
    states = list_agent_states(project)
    if not states:
        console.print(f"No claimed agents found for project '{project}'.")
        return

    table = Table(title=f"Cascade Status: {project}")
    table.add_column("Agent")
    table.add_column("Issue")
    table.add_column("Slug")
    table.add_column("Engine")
    table.add_column("Model")
    table.add_column("State")
    table.add_column("Gate")
    table.add_column("Worktree")

    for item in states:
        worktree_str = str(item.get("worktree", ""))
        worktree_path = Path(worktree_str) if worktree_str else None
        gate_result_path_str = str(item.get("gate_result_path", ""))
        gate_result: dict[str, object] | None = None
        if gate_result_path_str:
            gate_result = load_gate_result(Path(gate_result_path_str).parent)
        table.add_row(
            str(item.get("agent", "")),
            str(item.get("issue", "")),
            str(item.get("slug", "")),
            str(item.get("engine", "")),
            str(item.get("model", "")),
            str(item.get("state", "")),
            gate_status_line(gate_result, worktree_path),
            worktree_str,
        )
    console.print(table)


@app.command(name="show-prompt")
def show_prompt(
    agent: str,
    project: str = typer.Option(...),
    task: str | None = typer.Option(None, "--task"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
) -> None:
    prompt_path = resolve_prompt_path(get_agent_run_dir(project, agent), task=task, prompt_file=prompt_file)
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        print_error(f"Launch prompt not found: {prompt_path}")
        raise typer.Exit(1) from exc
    console.print(prompt)


@app.command()
def logs(agent: str, project: str = typer.Option(...), kind: LogKind = typer.Option(...)) -> None:
    run_dir = get_agent_run_dir(project, agent)
    paths = {
        LogKind.preflight: run_dir / "preflight.log",
        LogKind.prompt: run_dir / "launch_prompt.md",
        LogKind.mandate: run_dir / "mandate.md",
    }
    selected_path = paths[kind]
    try:
        console.print(selected_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        print_error(f"Requested {kind.value} log is missing: {selected_path}")
        raise typer.Exit(1) from exc


@app.command(help="Record a deterministic user decision note without calling OpenCode.")
def note(
    agent: str,
    project: str = typer.Option(...),
    message: str | None = typer.Option(None, "--message"),
) -> None:
    note_text = message if message is not None else typer.prompt("Note")
    if not note_text.strip():
        print_error("Note cannot be empty.")
        raise typer.Exit(1)

    try:
        load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)
    decisions_path = run_dir / "decisions.md"
    timestamp = timestamp_utc()
    append_markdown_entry(decisions_path, f"{timestamp} note", note_text)
    console.print(f"Saved note to {decisions_path}")


@app.command(help="Generate deterministic consolidated context for an agent run.")
def context(
    agent: str,
    project: str = typer.Option(...),
    print_output: bool = typer.Option(False, "--print"),
    include_diff: bool = typer.Option(False, "--include-diff"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)
    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_config = load_project_from_agent_state(agent_state)
    warnings: list[str] = []
    if project_config is not None:
        is_valid_location, location_message = validate_worktree_location(project_config, worktree)
        if not is_valid_location:
            warnings.append(location_message)
        branch_warning = validate_agent_branch(project_config, agent_state, worktree)
        if branch_warning is not None:
            warnings.append(branch_warning)
        warnings.extend(validate_instruction_files(project_config))
        configured_instruction_files = [str(path) for path in instruction_file_paths(project_config)]
    else:
        warnings.append("Unable to load project config from agent state project_file.")
        configured_instruction_files = []

    mandate = read_text(run_dir / "mandate.md")
    decisions = read_text(run_dir / "decisions.md")
    questions = read_text(run_dir / "questions.md")
    running_summary = read_text(run_dir / "running_summary.md")
    preflight_tail = read_tail_chars(run_dir / "preflight.log", 2000)

    git_status = get_git_status(worktree)
    git_diff_stat = get_git_diff_stat(worktree)
    git_branch = get_current_branch(worktree)

    # Load gate result and compute staleness deterministically.
    gate_result_path_str = str(agent_state.get("gate_result_path", ""))
    gate_result: dict[str, object] | None = None
    if gate_result_path_str:
        gate_result = load_gate_result(Path(gate_result_path_str).parent)
    gate_line = gate_status_line(gate_result, worktree)

    gate_section_lines: list[str] = [f"- Status: {gate_line}"]
    if gate_result is not None:
        gate_section_lines.append(f"- Timestamp: {gate_result.get('timestamp', '(unknown)')}")
        gate_section_lines.append(f"- Exit code: {gate_result.get('exit_code', '(unknown)')}")
        gate_section_lines.append(f"- Log: {gate_result.get('log_path', '(unknown)')}")
        failure_summary = gate_result.get("failure_summary")
        if failure_summary:
            gate_section_lines.append(f"\nFailure summary:\n{failure_summary}")
    gate_section = "\n".join(gate_section_lines)

    context_body = (
        f"# Cascade Context\n\n"
        f"## Agent Metadata\n"
        f"- Project: {agent_state.get('project', project)}\n"
        f"- Agent: {agent_state.get('agent', agent)}\n"
        f"- Issue: #{agent_state.get('issue', '')}\n"
        f"- Title: {agent_state.get('title', '')}\n"
        f"- Slug: {agent_state.get('slug', '')}\n"
        f"- State: {agent_state.get('state', '')}\n"
        f"- Worktree: {worktree}\n"
        f"- Current branch: {git_branch}\n\n"
        f"## Configured Instruction Files\n"
        + "\n".join(f"- {item}" for item in configured_instruction_files)
        + "\n\n"
        f"## Gate Result\n\n{gate_section}\n\n"
        f"## Mandate\n\n{mandate or '(none)'}\n\n"
        f"## Decisions\n\n{decisions or '(none)'}\n\n"
        f"## Questions\n\n{questions or '(none)'}\n\n"
        f"## Running Summary\n\n{running_summary or '(none)'}\n\n"
        f"## Git Status\n\n{git_status or '(clean)'}\n\n"
        f"## Git Diff Stat\n\n{git_diff_stat or '(none)'}\n\n"
        f"## Latest Preflight Log Tail\n\n{preflight_tail or '(none)'}\n\n"
    )
    if include_diff:
        context_body += f"## Git Diff Names\n\n{get_git_diff_names(worktree) or '(none)'}\n\n"
    if warnings:
        context_body += "## Warnings\n\n" + "\n".join(f"- {warning}" for warning in warnings) + "\n"

    context_path = run_dir / "context.md"
    context_path.write_text(context_body, encoding="utf-8")
    console.print(f"Context file: {context_path}")
    if print_output:
        console.print(context_body)


@app.command(help="Show deterministic git status and diff summary for the assigned worktree.")
def diff(
    agent: str,
    project: str = typer.Option(...),
    save: bool = typer.Option(False, "--save"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_config = load_project_from_agent_state(agent_state)
    if project_config is not None:
        emit_standards_warnings(project_config, agent_state, worktree)

    git_status = get_git_status(worktree)
    git_diff_stat = get_git_diff_stat(worktree)
    git_diff_names = get_git_diff_names(worktree)

    table = Table(title=f"Cascade Diff: {project}/{agent}")
    table.add_column("Section")
    table.add_column("Output")
    table.add_row("git status --short", git_status or "(clean)")
    table.add_row("git diff --stat", git_diff_stat or "(none)")
    table.add_row("git diff --name-only", git_diff_names or "(none)")
    console.print(table)

    if save:
        run_dir = get_agent_run_dir(project, agent)
        ensure_conversation_files(run_dir)
        diff_path = run_dir / "diff.md"
        body = (
            "# Diff Summary\n\n"
            f"## git status --short\n\n{git_status or '(clean)'}\n\n"
            f"## git diff --stat\n\n{git_diff_stat or '(none)'}\n\n"
            f"## git diff --name-only\n\n{git_diff_names or '(none)'}\n"
        )
        diff_path.write_text(body, encoding="utf-8")
        console.print(f"Saved diff summary: {diff_path}")


@app.command()
def mark(agent: str, project: str = typer.Option(...), state: AgentLifecycleState = typer.Option(...)) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    if state == AgentLifecycleState.closeout_ready:
        # Closeout readiness requires a passing, non-stale gate result.
        gate_result_path_str = str(agent_state.get("gate_result_path", ""))
        gate_result: dict[str, object] | None = None
        if gate_result_path_str:
            gate_result = load_gate_result(Path(gate_result_path_str).parent)

        if gate_result is None:
            print_error(
                "Cannot mark closeout_ready: no gate result found. "
                "Run `cascade preflight` first."
            )
            raise typer.Exit(1)

        if not gate_result.get("passed"):
            exit_code = gate_result.get("exit_code", "?")
            print_error(
                f"Cannot mark closeout_ready: last gate run failed (exit {exit_code}). "
                "Run `cascade preflight` and ensure it passes at the current HEAD/diff."
            )
            raise typer.Exit(1)

        worktree_str = str(agent_state.get("worktree", ""))
        if worktree_str:
            worktree = Path(worktree_str)
            is_stale, reason = check_gate_staleness(gate_result, worktree)
            if is_stale:
                print_error(
                    f"Cannot mark closeout_ready: gate result is stale — {reason} "
                    "Rerun `cascade preflight` at the current HEAD/diff."
                )
                raise typer.Exit(1)

    try:
        updated = update_agent_state(project, agent, state.value)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    console.print(f"Updated agent {agent} in project {project} to state '{updated['state']}'.")


@app.command(
    name="gate-status",
    help="Show latest gate result and staleness for an agent (deterministic, no model).",
)
def gate_status(agent: str, project: str = typer.Option(...)) -> None:
    """Read and display the saved gate result.  Exit 1 if gate failed or is stale."""
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    worktree_str = str(agent_state.get("worktree", ""))
    worktree = Path(worktree_str) if worktree_str else None

    gate_result_path_str = str(agent_state.get("gate_result_path", ""))
    gate_result: dict[str, object] | None = None
    if gate_result_path_str:
        gate_result = load_gate_result(Path(gate_result_path_str).parent)

    if gate_result is None:
        console.print("[yellow]No gate result found.[/yellow] Run `cascade preflight` first.")
        raise typer.Exit(1)

    table = Table(title=f"Gate Status: {project}/{agent}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Timestamp", str(gate_result.get("timestamp", "(unknown)")))
    table.add_row("Command", str(gate_result.get("command", "(unknown)")))
    table.add_row("Exit code", str(gate_result.get("exit_code", "(unknown)")))
    table.add_row("Passed", "yes" if gate_result.get("passed") else "no")
    table.add_row("Log file", str(gate_result.get("log_path", "(unknown)")))
    table.add_row("HEAD SHA", str(gate_result.get("git_head_sha", "(unknown)")))

    touched_raw = gate_result.get("touched_files", [])
    touched = list(touched_raw) if isinstance(touched_raw, list) else []
    table.add_row("Touched files", ", ".join(touched) if touched else "(none)")

    is_stale = False
    if worktree is not None and worktree.exists():
        is_stale, stale_reason = check_gate_staleness(gate_result, worktree)
        table.add_row("Stale", f"yes — {stale_reason}" if is_stale else "no")
    else:
        table.add_row("Stale", "(worktree not found; cannot check)")

    console.print(table)

    failure_summary = gate_result.get("failure_summary")
    if failure_summary:
        console.print("\n[yellow]Failure summary:[/yellow]")
        console.print(str(failure_summary))

    if not gate_result.get("passed") or is_stale:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# context-pack: deterministic context pack builder
# ---------------------------------------------------------------------------

_TASK_CHOICES = sorted(ALLOWED_TASKS)


@app.command(
    name="context-pack",
    help="Build a bounded, deterministic context pack for a model-backed task (no model call).",
)
def context_pack(
    agent: str,
    project: str = typer.Option(...),
    task: str = typer.Option(..., help=f"Task type: {', '.join(_TASK_CHOICES)}"),
    print_output: bool = typer.Option(False, "--print"),
    include_diff: bool = typer.Option(False, "--include-diff"),
) -> None:
    if task not in ALLOWED_TASKS:
        print_error(f"Unknown task '{task}'. Allowed: {', '.join(_TASK_CHOICES)}")
        raise typer.Exit(1)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_value = agent_state.get("project_file")
    if not isinstance(project_file_value, str) or not project_file_value:
        print_error("Agent state does not include project_file. Re-claim the issue.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(Path(project_file_value))
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    try:
        pack = build_context_pack(project_config, agent_state, task, run_dir, include_diff=include_diff)
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    md_path, json_path = save_context_pack(run_dir, pack)

    console.print(f"Context pack  : {md_path}")
    console.print(f"Metadata      : {json_path}")
    console.print(f"Est. tokens   : ~{pack.estimated_input_tokens:,}")
    console.print(f"Budget        : {pack.max_input_tokens:,}")
    if pack.truncated:
        console.print("[yellow]Warning: context was truncated to fit token budget.[/yellow]")
    for warning in pack.warnings:
        print_warning(warning)
    if print_output:
        console.print(pack.body)


# ---------------------------------------------------------------------------
# estimate-cost: deterministic cost estimator
# ---------------------------------------------------------------------------


@app.command(
    name="estimate-cost",
    help="Estimate model cost for a task based on context pack and profile (no model call).",
)
def estimate_cost_cmd(
    agent: str,
    project: str = typer.Option(...),
    task: str = typer.Option(...),
    profile: str = typer.Option(...),
    expected_output_tokens: int = typer.Option(0, "--expected-output-tokens"),
) -> None:
    if task not in ALLOWED_TASKS:
        print_error(f"Unknown task '{task}'. Allowed: {', '.join(_TASK_CHOICES)}")
        raise typer.Exit(1)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_value = agent_state.get("project_file")
    if not isinstance(project_file_value, str) or not project_file_value:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(Path(project_file_value))
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    try:
        model_profile = get_model_profile(project_config, profile)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    # Resolve context pack (build fresh or read existing)
    run_dir = get_agent_run_dir(project, agent)
    context_pack_md = run_dir / f"context_{task}.md"
    if context_pack_md.exists():
        input_tokens = estimate_tokens(context_pack_md.read_text(encoding="utf-8"))
    else:
        try:
            pack = build_context_pack(project_config, agent_state, task, run_dir)
            save_context_pack(run_dir, pack)
            input_tokens = pack.estimated_input_tokens
        except ValueError as exc:
            print_error(str(exc))
            raise typer.Exit(1) from exc

    output_tokens = expected_output_tokens or DEFAULT_EXPECTED_OUTPUT_TOKENS.get(task, 10000)
    lines = cost_summary_lines(input_tokens, output_tokens, model_profile, profile)
    for line in lines:
        console.print(line)


# ---------------------------------------------------------------------------
# prepare-model-call: build prompt + metadata without calling a model
# ---------------------------------------------------------------------------


@app.command(
    name="prepare-model-call",
    help="Build task prompt and cost metadata for a model-backed call (no model call).",
)
def prepare_model_call(
    agent: str,
    project: str = typer.Option(...),
    task: str = typer.Option(...),
    profile: str = typer.Option(...),
    include_diff: bool = typer.Option(False, "--include-diff"),
) -> None:
    import json as _json

    if task not in ALLOWED_TASKS:
        print_error(f"Unknown task '{task}'. Allowed: {', '.join(_TASK_CHOICES)}")
        raise typer.Exit(1)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_value = agent_state.get("project_file")
    if not isinstance(project_file_value, str) or not project_file_value:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(Path(project_file_value))
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    try:
        model_profile = get_model_profile(project_config, profile)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)

    # Build context pack
    try:
        pack = build_context_pack(project_config, agent_state, task, run_dir, include_diff=include_diff)
        save_context_pack(run_dir, pack)
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    # Build prompt
    prompt_body = build_task_prompt(pack.body, task)
    prompt_path = run_dir / f"{task}_prompt.md"
    prompt_path.write_text(prompt_body, encoding="utf-8")

    # Cost estimate
    input_tokens = pack.estimated_input_tokens
    output_tokens = DEFAULT_EXPECTED_OUTPUT_TOKENS.get(task, 10000)
    cost_usd = estimate_cost(input_tokens, output_tokens, model_profile)
    model_id = model_id_for_opencode(model_profile)
    ts = timestamp_utc()

    metadata: dict[str, object] = {
        "task_type": task,
        "profile": profile,
        "model_id": model_id,
        "estimated_input_tokens": input_tokens,
        "expected_output_tokens": output_tokens,
        "estimated_cost_usd": round(cost_usd, 6),
        "generated_at": ts,
    }
    meta_path = run_dir / f"{task}_model_call.json"
    meta_path.write_text(_json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    console.print(f"Prompt file   : {prompt_path}")
    console.print(f"Metadata      : {meta_path}")
    console.print(f"Model         : {model_id}")
    console.print(f"Est. tokens   : ~{input_tokens:,} in / ~{output_tokens:,} out")
    from cascade.costs import format_cost
    console.print(f"Est. cost     : {format_cost(cost_usd)}")
    console.print(
        "Tip: pass this model string to OpenCode with "
        f"`opencode . --model {model_id}`"
    )
    console.print("Note: costs are approximations — verify at https://openrouter.ai/models")


# ---------------------------------------------------------------------------
# gate-summary: classify and display latest gate failure
# ---------------------------------------------------------------------------


@app.command(
    name="gate-summary",
    help="Classify the latest gate failure from saved logs (deterministic, no model).",
)
def gate_summary(agent: str, project: str = typer.Option(...)) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_value = agent_state.get("project_file")
    if isinstance(project_file_value, str) and project_file_value:
        try:
            project_config = load_project_config(Path(project_file_value))
            finding = detect_missing_mandate_metadata(project_config, agent_state)
        except ConfigError:
            finding = None
        if finding is not None:
            table = Table(title=f"Gate Summary: {project}/{agent}")
            table.add_column("Field")
            table.add_column("Value")
            table.add_row("Detected failure", "yes")
            table.add_row("Hook / check", "mandate-metadata")
            table.add_row("Category", "workflow")
            table.add_row("Model recommended", "no")
            table.add_row("Suggested action", f"cascade repair {agent} --project {project}")
            console.print(table)
            return

    run_dir = get_agent_run_dir(project, agent)
    log_path = run_dir / "preflight.log"

    try:
        log_content = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print_error(f"No preflight log found at {log_path}. Run `cascade preflight` first.")
        raise typer.Exit(1)

    classification = classify_gate_failure(log_content)

    table = Table(title=f"Gate Summary: {project}/{agent}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Detected failure", "yes" if classification["detected"] else "no")
    table.add_row("Hook / check", str(classification.get("hook") or "(unknown)"))
    table.add_row("Category", str(classification.get("category", "unknown")))
    dirty_file_path = str(classification.get("dirty_file_path") or "")
    if dirty_file_path:
        table.add_row("Dirty file path", dirty_file_path)
    table.add_row("Model recommended", "yes" if classification["model_recommended"] else "no")
    table.add_row("Suggested action", str(classification.get("suggested_no_model_action", "")))
    console.print(table)


# ---------------------------------------------------------------------------
# gate-fix: headless OpenRouter-based auto-fix for code-fixable gate failures
# ---------------------------------------------------------------------------


@app.command(
    name="gate-fix",
    help="Headless OpenRouter-based auto-fix loop for code-fixable gate failures.",
)
def gate_fix(
    agent: str,
    project: str = typer.Option(...),
    profile: str | None = typer.Option(None, "--profile", help="Gate-fix profile (default: cheap-fixer)"),
    max_attempts: int = typer.Option(3, "--max-attempts", help="Maximum fix attempts"),
    max_estimated_cost: float = typer.Option(0.25, "--max-estimated-cost", "--max-cost", help="Cost cap in USD"),
    batch_mode: GateFixBatchMode = typer.Option(
        GateFixBatchMode.FILE,
        "--batch-mode",
        help="Fix batch granularity: file (default), group, or broad.",
    ),
    fallback_model: str | None = typer.Option(None, "--fallback-model", help="Fallback model ID"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream model output to terminal"),
    debug: bool = typer.Option(False, "--debug-openrouter", help="Enable OpenRouter debug logging"),
    failure_context_file: Path | None = typer.Option(None, "--failure-context-file", help="Optional explicit failure context JSON file for this run."),
) -> None:
    """Automatically fix code-related gate failures using OpenRouter.
    
    This command:
    1. Loads the latest gate result and failure log
    2. Classifies the failure (deterministic vs code-fixable)
    3. If code-fixable, starts a bounded model-fix loop
    4. Streams model progress in real-time to terminal
    5. Applies patches and re-runs the gate
    6. Saves full artifacts and logs
    
    Usage:
        cascade gate-fix a3 --project jungle
        cascade gate-fix a3 --project jungle --max-attempts 5 --max-cost 1.00
    """
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        print_error("Agent state does not include project_file.")
        raise typer.Exit(1)

    try:
        project_config = load_project_config(project_file_path)
    except ConfigError as exc:
        print_error(f"Failed to load project config: {exc}")
        raise typer.Exit(1) from exc

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    active_failure = _resolve_gate_fix_failure_source(
        run_dir=run_dir,
        explicit_context_file=failure_context_file,
    )
    if active_failure is None:
        print_error(
            "No active failure context found. Run closeout-prep/commit, check/preflight, closeout, or provide --failure-context-file."
        )
        raise typer.Exit(1)

    gate_command = str(active_failure.get("command") or "make preflight")
    failing_hook = str(active_failure.get("hook") or "unknown")
    log_content = str(active_failure.get("log") or "")
    if not log_content.strip():
        log_path_value = str(active_failure.get("log_path") or "")
        if log_path_value:
            try:
                log_content = Path(log_path_value).read_text(encoding="utf-8")
            except FileNotFoundError:
                log_content = ""
    if not log_content.strip():
        print_error("Active failure context is missing readable failure logs.")
        raise typer.Exit(1)

    gate_result_raw = active_failure.get("gate_result")
    gate_result: dict[str, object]
    if isinstance(gate_result_raw, dict):
        gate_result = dict(gate_result_raw)
    else:
        touched_raw = active_failure.get("touched_files", [])
        touched_files = [str(item) for item in touched_raw if isinstance(item, str)] if isinstance(touched_raw, list) else []
        gate_result = {
            "command": gate_command,
            "hook": failing_hook,
            "log_path": str(active_failure.get("log_path") or ""),
            "touched_files": touched_files,
            "passed": False,
        }

    category = classify_failure_as_model_fixable(log_content, failing_hook)

    if isinstance(batch_mode, GateFixBatchMode):
        resolved_batch_mode = batch_mode
    elif isinstance(batch_mode, str):
        try:
            resolved_batch_mode = GateFixBatchMode(batch_mode)
        except ValueError:
            resolved_batch_mode = GateFixBatchMode.FILE
    else:
        resolved_batch_mode = GateFixBatchMode.FILE

    print(f"[gate-fix] Source: {active_failure.get('source', 'unknown')}")
    print(f"[gate-fix] Category: {category.value}")

    if not is_model_fixable(category):
        print_error(f"Failure category '{category.value}' is not model-fixable. Use `cascade repair` instead.")
        raise typer.Exit(1)

    # Load model profile
    try:
        model_profile = resolve_gate_fix_model_profile(project_config, profile)
    except ConfigError:
        model_profile = get_default_gate_fix_model()
        selected_profile = profile or "cheap-fixer"
        console.print(f"[yellow]Warning:[/yellow] Profile '{selected_profile}' not found, using default cheap fixer model.")
    else:
        selected_profile = profile or "cheap-fixer"

    # Build config
    fallback_models_list = []
    if fallback_model:
        fallback_models_list.append(fallback_model)
    else:
        # Add built-in fallbacks
        fallback_profs = get_gate_fix_fallback_models()
        fallback_models_list.extend([p.model for p in fallback_profs])
    
    fix_config = GateFixConfig(
        model=model_profile.model,
        max_attempts=max_attempts,
        max_estimated_cost_usd=max_estimated_cost,
        stream=stream,
        debug=debug,
        fallback_models=fallback_models_list,
        batch_mode=resolved_batch_mode,
    )

    model_profiles_by_id = {model_profile.model: model_profile}
    for fallback_profile in get_gate_fix_fallback_models():
        model_profiles_by_id.setdefault(fallback_profile.model, fallback_profile)

    # Resolve mandate slug
    slug = str(agent_state.get("slug", agent))

    console.print(f"\n[bold]Gate Fix Loop[/bold]")
    console.print(f"  Agent: {agent}")
    console.print(f"  Project: {project}")
    console.print(f"  Worktree: {worktree}")
    console.print(f"  Category: {category.value}")
    console.print(f"  Profile: {selected_profile}")
    console.print(f"  Model: {model_profile.model}")
    console.print(f"  Max attempts: {max_attempts}")
    console.print(f"  Cost cap: ${max_estimated_cost:.2f}\n")
    console.print(f"  Batch mode: {resolved_batch_mode.value}\n")

    # Run fix loop
    result = run_gate_fix_loop(
        worktree=worktree,
        project_name=project,
        agent=agent,
        mandate_slug=slug,
        gate_command=gate_command,
        failing_hook=failing_hook,
        failing_log=log_content,
        failing_category=category,
        config=fix_config,
        model_profile=model_profile,
        run_dir=run_dir,
        gate_result=gate_result,
        model_profiles_by_id=model_profiles_by_id,
        failure_source=str(active_failure.get("source") or ""),
    )

    # Save summary
    summary_path = save_gate_fix_summary(run_dir, result)
    console.print(f"\n[bold]Summary saved:[/bold] {summary_path}\n")
    
    # Print result
    if result.success:
        _clear_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME)
        console.print("[bold green]✓ Gate passed![/bold green]")
        console.print(f"  Attempts: {len(result.attempts)}")
        console.print(f"  Total cost: ${result.total_estimated_cost:.4f}")
        raise typer.Exit(0)
    else:
        console.print(f"[bold red]✗ Fix failed[/bold red]")
        console.print(f"  Attempts: {len(result.attempts)}")
        console.print(f"  Reason: {result.stop_reason}")
        console.print(f"  Total cost: ${result.total_estimated_cost:.4f}")
        if result.error_message:
            console.print(f"  Error: {result.error_message}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# budget-status: show attempt counts, context estimates, gate state
# ---------------------------------------------------------------------------


@app.command(
    name="budget-status",
    help="Show attempt counts, context pack estimates, and gate state (no model).",
)
def budget_status(agent: str, project: str = typer.Option(...)) -> None:
    import json as _json

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    worktree_str = str(agent_state.get("worktree", ""))
    worktree = Path(worktree_str) if worktree_str else None

    # Gate result
    gate_result_path_str = str(agent_state.get("gate_result_path", ""))
    gate_result: dict[str, object] | None = None
    if gate_result_path_str:
        gate_result = load_gate_result(Path(gate_result_path_str).parent)
    gate_line = gate_status_line(gate_result, worktree)

    run_dir = get_agent_run_dir(project, agent)

    # Attempt tracking
    attempts_raw = agent_state.get("attempts", {})
    attempts = attempts_raw if isinstance(attempts_raw, dict) else {}

    table = Table(title=f"Budget Status: {project}/{agent}")
    table.add_column("Item")
    table.add_column("Value")

    table.add_row("Agent state", str(agent_state.get("state", "(unknown)")))
    table.add_row("Gate", gate_line)

    # Attempt counts
    for task in ("plan", "implement", "diagnose", "fix", "review", "summarize"):
        task_entry = attempts.get(task, {})
        count = int(task_entry.get("count", 0)) if isinstance(task_entry, dict) else 0
        last_profile = str(task_entry.get("last_profile") or "(none)") if isinstance(task_entry, dict) else "(none)"
        table.add_row(f"Attempts: {task}", f"{count} (last profile: {last_profile})")

    # Context pack token estimates from saved JSON files
    for task in _TASK_CHOICES:
        meta_json = run_dir / f"context_{task}.json"
        if meta_json.exists():
            try:
                meta = _json.loads(meta_json.read_text(encoding="utf-8"))
                est = meta.get("estimated_input_tokens", "?")
                truncated = "(truncated)" if meta.get("truncated") else ""
                table.add_row(f"Context pack: {task}", f"~{est:,} tokens {truncated}")
            except (_json.JSONDecodeError, ValueError):
                pass

    # Model call metadata
    for task in _TASK_CHOICES:
        call_meta_json = run_dir / f"{task}_model_call.json"
        if call_meta_json.exists():
            try:
                call_meta = _json.loads(call_meta_json.read_text(encoding="utf-8"))
                cost = call_meta.get("estimated_cost_usd", "?")
                model_id = call_meta.get("model_id", "?")
                table.add_row(f"Est. cost: {task}", f"~${cost:.4f} USD ({model_id})")
            except (_json.JSONDecodeError, ValueError):
                pass

    console.print(table)


@app.command(help="List deterministic and model-backed command capabilities.")
def capabilities() -> None:
    table = Table(title="Cascade Capabilities")
    table.add_column("Command")
    table.add_column("Category")
    table.add_column("Requires OpenCode")
    table.add_column("Requires gh")
    table.add_column("Mutates Target Repo")
    table.add_column("Description")

    for command, meta in NO_MODEL_COMMANDS.items():
        table.add_row(
            command,
            "deterministic",
            "yes" if meta["requires_opencode"] else "no",
            "yes" if meta["requires_gh"] else "no",
            "yes" if meta["mutates_target_repo"] else "no",
            meta["description"],
        )
    for command, meta in MODEL_BACKED_COMMANDS.items():
        table.add_row(
            command,
            "model-backed",
            "yes" if meta["requires_opencode"] else "no",
            "yes" if meta["requires_gh"] else "no",
            "yes" if meta["mutates_target_repo"] else "no",
            meta["description"],
        )
    for command, meta in PLANNED_MODEL_BACKED_COMMANDS.items():
        table.add_row(
            command,
            "planned",
            "yes" if meta["requires_opencode"] else "no",
            "yes" if meta["requires_gh"] else "no",
            "yes" if meta["mutates_target_repo"] else "no",
            meta["description"],
        )
    console.print(table)


@app.command()
def doctor(project_file: Path = typer.Option(..., exists=True, dir_okay=False, readable=True)) -> None:
    checks = run_doctor_checks(project_file)
    table = Table(title="Cascade Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Details")
    for check in checks:
        table.add_row(check.name, check.status, check.details)
    console.print(table)
    if has_failures(checks):
        raise typer.Exit(1)


@app.command(
    name="opencode-setup",
    help="Deterministically verify OpenCode host PATH integration and repair shell startup files.",
)
def opencode_setup(
    home: Path | None = typer.Option(None, "--home", file_okay=False, dir_okay=True),
) -> None:
    result = ensure_opencode_host_path_setup(home)

    table = Table(title="OpenCode Host Setup")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("Resolved binary", str(result.resolved_binary_path) if result.resolved_binary_path else "(not found)")
    table.add_row("Resolved from", result.resolved_from)
    table.add_row("Symlink path", str(result.symlink_path))
    table.add_row("Symlink created", "yes" if result.symlink_created else "no")
    table.add_row("Symlink updated", "yes" if result.symlink_updated else "no")
    table.add_row("Symlink already correct", "yes" if result.symlink_already_correct else "no")
    table.add_row("Symlink blocked", "yes" if result.symlink_blocked else "no")
    table.add_row("Updated ~/.bashrc", "yes" if result.bashrc_updated else "no")
    table.add_row("Updated ~/.bash_profile", "yes" if result.bash_profile_updated else "no")
    table.add_row(
        "Added ~/.bash_profile -> ~/.bashrc source",
        "yes" if result.bash_profile_sources_bashrc_added else "no",
    )
    console.print(table)

    if result.resolved_binary_path is None:
        print_error(
            "OpenCode binary not found on PATH and not found under HOME. Install OpenCode, then rerun: "
            "cascade opencode-setup"
        )
        raise typer.Exit(1)

    if result.symlink_blocked:
        print_error(
            f"Unable to create stable symlink at {result.symlink_path}: path exists and is not a symlink. "
            "Move that file and rerun `cascade opencode-setup`."
        )
        raise typer.Exit(1)


@app.command()
def preflight(
    agent: str,
    project: str = typer.Option(...),
    verbose: bool = typer.Option(False, "--verbose"),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_path = _resolve_project_file_from_state(agent_state)
    if project_file_path is None:
        print_error(
            "Agent state does not include project_file. Re-claim the issue or add project_file manually."
        )
        raise typer.Exit(1)

    try:
        project_config = load_project_config(project_file_path)
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    agent_state = _migrate_agent_state_if_needed(
        project_name=project,
        agent=agent,
        agent_state=agent_state,
        project_config=project_config,
    )

    if project_config.commands.preflight is None:
        print_error("Project config does not define commands.preflight.")
        raise typer.Exit(1)

    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)

    try:
        validate_mandate_metadata_before_preflight(
            project_config,
            worktree,
            str(agent_state["slug"]),
            agent=agent,
            issue=int(agent_state["issue"]),
            title=str(agent_state["title"]),
        )
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    workspace_link_result = repair_missing_workspace_links(
        project_config,
        agent_state,
        dry_run=False,
    )
    if not workspace_link_result.success:
        print_error(workspace_link_result.message)
        console.print(f"Repair log: {workspace_link_result.log_path}")
        raise typer.Exit(1)

    run_dir = get_agent_run_dir(project, agent)
    log_path = run_dir / "preflight.log"
    preflight_command = project_config.commands.preflight.format(
        agent=agent,
        slug=str(agent_state["slug"]),
        issue=str(agent_state["issue"]),
        project=project,
    )
    preflight_command = format_command_template(
        project_config.commands.preflight,
        project=project_config,
        agent=agent,
        slug=str(agent_state["slug"]),
        issue=int(agent_state["issue"]),
        title=str(agent_state.get("title", "")),
        canonical_mandate=get_agent_run_dir(project, agent) / "mandate.md",
    )
    started = time.monotonic()
    result = _run_preflight_command(
        command=preflight_command,
        worktree=worktree,
        log_path=log_path,
        verbose=verbose,
        watch=watch,
    )
    elapsed = time.monotonic() - started
    preflight_timestamp = timestamp_utc()
    log_content = result.output
    log_path.write_text(
        _build_preflight_log_content(preflight_timestamp, preflight_command, result.returncode, log_content),
        encoding="utf-8",
    )

    passed = result.returncode == 0

    # Capture git state at the moment the gate ran so staleness can be detected later.
    head_sha = get_git_head_sha(worktree)
    diff_fp = get_diff_fingerprint(worktree)
    touched_files = get_touched_files(worktree)

    gate_data: dict[str, object] = {
        "timestamp": preflight_timestamp,
        "command": preflight_command,
        "exit_code": result.returncode,
        "passed": passed,
        "log_path": str(log_path),
        "git_head_sha": head_sha,
        "diff_fingerprint": diff_fp,
        "touched_files": touched_files,
        "failure_summary": (
            None
            if passed
            else build_failure_summary(
                {
                    "command": preflight_command,
                    "exit_code": result.returncode,
                    "log_path": str(log_path),
                    "touched_files": touched_files,
                },
                log_content,
            )
        ),
    }
    gate_result_path = save_gate_result(run_dir, gate_data)

    new_state = (
        AgentLifecycleState.preflight_passed.value
        if passed
        else AgentLifecycleState.preflight_failed.value
    )
    updated_state = load_agent_state(project, agent)
    updated_state["state"] = new_state
    updated_state["preflight_last_run_at"] = preflight_timestamp
    updated_state["preflight_last_exit_code"] = result.returncode
    updated_state["preflight_last_log"] = str(log_path)
    updated_state["gate_result_path"] = str(gate_result_path)
    save_agent_state(project, agent, updated_state)

    console.print(f"Preflight log  : {log_path}")
    console.print(f"Gate result    : {gate_result_path}")
    console.print(f"Elapsed        : {elapsed:.1f}s")
    if passed:
        _clear_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME)
    if not passed:
        preflight_failure_context = {
            "source": "preflight",
            "timestamp": preflight_timestamp,
            "command": preflight_command,
            "hook": str(classify_gate_failure(log_content).get("hook") or "unknown"),
            "log": log_content,
            "log_path": str(log_path),
            "touched_files": touched_files,
            "gate_result": gate_data,
        }
        _save_failure_context(run_dir, _CURRENT_FAILURE_CONTEXT_FILENAME, preflight_failure_context)
        # Always surface the failure tail regardless of verbosity so users do
        # not have to re-run manually with --verbose just to see the error.
        if not watch:
            _emit_preflight_failure_tail(log_content)
        if _is_opaque_preflight_log(log_content):
            console.print(
                "[yellow]Warning: preflight log appears thin — only a make error wrapper "
                "was captured.  The real error may have been written to a sub-process log "
                f"inside the worktree.  Re-run with --verbose for streaming output:[/yellow]"
            )
            console.print(
                f"  cascade preflight {agent} --project {project} --verbose",
                markup=False,
            )
        failure_summary = gate_data.get("failure_summary")
        if failure_summary:
            console.print("\n[yellow]Failure summary:[/yellow]")
            console.print(str(failure_summary))
        print_error(f"Preflight failed with exit code {result.returncode}.")
        raise typer.Exit(1)


@app.command(help="Launch interactive OpenCode session for this agent.")
def chat(
    agent: str,
    project: str = typer.Option(...),
    mode: OpenCodeMode | None = typer.Option(None, "--mode"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_config = load_project_from_agent_state(agent_state)
    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)
    model = str(agent_state["model"])
    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)

    launch_prompt_path = run_dir / "launch_prompt.md"
    running_summary_path = run_dir / "running_summary.md"
    decisions_path = run_dir / "decisions.md"
    session_id_path = run_dir / "opencode_session_id.txt"

    console.print(f"Launch prompt: {launch_prompt_path}")
    console.print(f"Running summary: {running_summary_path}")
    console.print(f"Decisions: {decisions_path}")
    if read_text(session_id_path):
        console.print(
            f"Session hint: specific session resume may be available with ID stored in {session_id_path}"
        )

    try:
        ensure_opencode_available()
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    result = subprocess.run(build_interactive_command(model, mode=mode), cwd=worktree, check=False)
    if result.returncode != 0:
        print_error(
            f"OpenCode exited with status {result.returncode}. If this appears flag-related, check `opencode --help`."
        )
        raise typer.Exit(result.returncode)

    agent_state["last_mode"] = mode.value if mode is not None else None
    agent_state["last_interaction_at"] = timestamp_utc()
    save_agent_state(project, agent, agent_state)


@app.command(help="Ask the model a question through OpenCode.")
def ask(
    agent: str,
    question: str = typer.Argument(...),
    project: str = typer.Option(...),
    mode: OpenCodeMode | None = typer.Option(None, "--mode"),
    no_continue: bool = typer.Option(False, "--no-continue"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)
    project_config = load_project_from_agent_state(agent_state)
    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)

    summary = read_text(run_dir / "running_summary.md")
    decisions = read_text(run_dir / "decisions.md")
    prompt = build_ask_prompt(
        question=question,
        issue=int(agent_state["issue"]),
        title=str(agent_state["title"]),
        running_summary=summary,
        decisions=decisions,
    )

    try:
        ensure_opencode_available()
        output = run_prompt(
            prompt=prompt,
            worktree=worktree,
            model=str(agent_state["model"]),
            mode=mode,
            use_continue=not no_continue,
        )
    except OpenCodeError as exc:
        if no_continue:
            print_error(str(exc))
            raise typer.Exit(1) from exc
        fallback_prompt = (
            prompt
            + "\n\nContinue flag failed; continue this answer using only the included conversation capsule context."
        )
        try:
            output = run_prompt(
                prompt=fallback_prompt,
                worktree=worktree,
                model=str(agent_state["model"]),
                mode=mode,
                use_continue=False,
            )
        except OpenCodeError as fallback_exc:
            print_error(str(fallback_exc))
            raise typer.Exit(1) from fallback_exc

    timestamp = timestamp_utc()
    append_markdown_entry(run_dir / "questions.md", f"{timestamp} user question", question)
    append_markdown_entry(run_dir / "transcript.md", f"{timestamp} user", question)
    append_markdown_entry(run_dir / "transcript.md", f"{timestamp} agent", output)
    console.print(output)

    agent_state["last_mode"] = mode.value if mode is not None else None
    agent_state["last_interaction_at"] = timestamp
    save_agent_state(project, agent, agent_state)


@app.command(help="Ask the model to process a clarification through OpenCode.")
def clarify(
    agent: str,
    project: str = typer.Option(...),
    mode: OpenCodeMode | None = typer.Option(None, "--mode"),
    message: str | None = typer.Option(None, "--message"),
) -> None:
    clarification = message if message is not None else typer.prompt("Clarification")
    if not clarification.strip():
        print_error("Clarification cannot be empty.")
        raise typer.Exit(1)

    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)
    worktree = Path(str(agent_state["worktree"]))
    if not worktree.exists():
        print_error(f"Worktree does not exist: {worktree}")
        raise typer.Exit(1)

    timestamp = timestamp_utc()
    append_markdown_entry(run_dir / "decisions.md", f"{timestamp} clarification", clarification)

    continuation_prompt = (
        "The user has clarified the following. Update your plan and ask any remaining "
        "blocking questions before editing.\n\n"
        f"Clarification:\n{clarification.strip()}"
    )
    try:
        ensure_opencode_available()
        output = run_prompt(
            prompt=continuation_prompt,
            worktree=worktree,
            model=str(agent_state["model"]),
            mode=mode,
            use_continue=True,
        )
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    append_markdown_entry(run_dir / "transcript.md", f"{timestamp} user clarification", clarification)
    append_markdown_entry(run_dir / "transcript.md", f"{timestamp} agent", output)
    console.print(output)

    agent_state["last_mode"] = mode.value if mode is not None else None
    agent_state["last_interaction_at"] = timestamp
    save_agent_state(project, agent, agent_state)


@app.command(help="Ask the model to summarize current work through OpenCode.")
def summarize(
    agent: str,
    project: str = typer.Option(...),
    mode: OpenCodeMode | None = typer.Option(None, "--mode"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)
    project_config = load_project_from_agent_state(agent_state)
    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)

    mandate = read_tail_chars(run_dir / "mandate.md", 5000)
    transcript_excerpt = read_tail_chars(run_dir / "transcript.md", 6000)
    decisions = read_tail_chars(run_dir / "decisions.md", 4000)
    try:
        git_status_result = run_command("git status --short", cwd=worktree)
        git_status = git_status_result.stdout.strip()
    except CommandError:
        git_status = "(unable to read git status)"

    prompt = build_summarize_prompt(
        issue=int(agent_state["issue"]),
        title=str(agent_state["title"]),
        mandate=mandate,
        git_status=git_status,
        transcript_excerpt=transcript_excerpt,
        decisions=decisions,
    )

    try:
        ensure_opencode_available()
        output = run_prompt(
            prompt=prompt,
            worktree=worktree,
            model=str(agent_state["model"]),
            mode=mode,
            use_continue=True,
        )
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    summary_path = run_dir / "running_summary.md"
    summary_path.write_text(output, encoding="utf-8")
    append_markdown_entry(run_dir / "transcript.md", f"{timestamp_utc()} summary refresh", output)
    console.print(output)

    agent_state["last_mode"] = mode.value if mode is not None else None
    agent_state["last_interaction_at"] = timestamp_utc()
    save_agent_state(project, agent, agent_state)


@app.command(name="continue", help="Prepare continuation prompt and launch OpenCode.")
def continue_agent(
    agent: str,
    project: str = typer.Option(...),
    print_prompt: bool = typer.Option(False, "--print-prompt"),
    mode: OpenCodeMode | None = typer.Option(None, "--mode"),
) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    run_dir = get_agent_run_dir(project, agent)
    ensure_conversation_files(run_dir)
    project_config = load_project_from_agent_state(agent_state)
    try:
        worktree = require_existing_worktree(agent_state)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    emit_standards_warnings(project_config, agent_state, worktree)

    continue_prompt = build_continue_prompt(
        issue=int(agent_state["issue"]),
        title=str(agent_state["title"]),
        mandate=read_tail_chars(run_dir / "mandate.md", 5000),
        running_summary=read_tail_chars(run_dir / "running_summary.md", 5000),
        decisions=read_tail_chars(run_dir / "decisions.md", 5000),
        questions=read_tail_chars(run_dir / "questions.md", 5000),
        preflight_log=read_tail_chars(run_dir / "preflight.log", 5000),
    )
    continue_prompt_path = run_dir / "continue_prompt.md"
    continue_prompt_path.write_text(continue_prompt, encoding="utf-8")
    console.print(f"Continuation prompt: {continue_prompt_path}")
    if print_prompt:
        console.print(continue_prompt)

    run_agent(
        agent=agent,
        project=project,
        print_prompt=print_prompt,
        task="continue",
        mode=mode,
    )


def default_model_name(project: ProjectConfig) -> str:
    default_profile = project.models.default
    if default_profile is None:
        raise ConfigError("Project config is missing models.default and no --model override was provided.")
    return f"{default_profile.provider}/{default_profile.model}"


def build_branch_name(project: ProjectConfig, agent: str, slug: str) -> str:
    template = project.branches.agent_branch_template
    if template is None:
        return f"agent/{agent}/{slug}"
    return template.format(agent=agent, slug=slug)


def main() -> None:
    load_repo_env_defaults()
    app()


if __name__ == "__main__":
    main()