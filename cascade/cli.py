from __future__ import annotations

from dataclasses import dataclass
import platform
import json
import shlex
import subprocess
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
    instruction_file_paths,
    load_project_config,
    model_id_for_opencode,
    resolve_model_for_task,
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
from cascade.opencode import (
    OpenCodeError,
    OpenCodeMode,
    build_interactive_command,
    ensure_opencode_available,
    run_prompt,
)
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
    get_diff_fingerprint,
    get_git_head_sha,
    get_touched_files,
    gate_status_line,
    load_gate_result,
    save_gate_result,
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


class AgentLifecycleState(str, Enum):
    claimed = "claimed"
    running = "running"
    blocked = "blocked"
    implementation_done = "implementation_done"
    preflight_running = "preflight_running"
    preflight_failed = "preflight_failed"
    preflight_passed = "preflight_passed"
    closeout_ready = "closeout_ready"
    closed = "closed"


class LogKind(str, Enum):
    preflight = "preflight"
    prompt = "prompt"
    mandate = "mandate"


class RepairKind(str, Enum):
    auto = "auto"
    missing_mandate_metadata = "missing-mandate-metadata"


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


def print_error(message: str) -> None:
    console.print(f"[red]Error:[/red] {message}")


def print_warning(message: str) -> None:
    console.print(f"[yellow]Warning:[/yellow] {message}")


def load_project_from_agent_state(agent_state: dict[str, object]) -> ProjectConfig | None:
    project_file = agent_state.get("project_file")
    if not isinstance(project_file, str) or not project_file:
        return None
    try:
        return load_project_config(Path(project_file))
    except ConfigError:
        return None


def require_existing_worktree(agent_state: dict[str, object]) -> Path:
    worktree = Path(str(agent_state["worktree"]))
    if not worktree.exists():
        raise FileNotFoundError(f"Worktree does not exist: {worktree}")
    return worktree


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


def resolve_prompt_path(run_dir: Path, task: str | None = None, prompt_file: Path | None = None) -> Path:
    if prompt_file is not None:
        path = prompt_file
    elif task is not None:
        path = run_dir / f"{task}_prompt.md"
    else:
        path = run_dir / "launch_prompt.md"

    if not path.exists():
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
    return current_branch == expected or current_branch.startswith("agent/")


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
                message = f"Mandate metadata already exists at {metadata}. No repair is needed."
                log_lines.append(message)
                log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                return RepairResult(
                    kind=RepairKind.missing_mandate_metadata,
                    success=True,
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

    if not finding.metadata_path.exists():
        message = (
            "Repair command completed but mandate metadata is still missing: "
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


def run_repair(
    project_config: ProjectConfig,
    agent_state: dict[str, object],
    *,
    kind: RepairKind,
    dry_run: bool,
    allow_stash: bool,
    active_branch_override: str | None,
) -> RepairResult:
    if kind in {RepairKind.auto, RepairKind.missing_mandate_metadata}:
        return repair_missing_mandate_metadata(
            project_config,
            agent_state,
            dry_run=dry_run,
            allow_stash=allow_stash,
            active_branch_override=active_branch_override,
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

    agent_state = {
        "project": project.name,
        "agent": agent,
        "issue": issue_number,
        "title": title,
        "slug": slug,
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
            selected_profile = next(
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
    run_agent(
        agent=agent,
        project=project_config.name,
        print_prompt=False,
        task=task if task_prompt.exists() else None,
    )


@app.command(help="Show diff summary, run preflight, and recommend finish or fix.")
def check(
    agent: str,
    project: str = typer.Option(...),
    repair: bool = typer.Option(False, "--repair"),
    repair_only: bool = typer.Option(False, "--repair-only"),
    active_branch: str | None = typer.Option(None, "--active-branch"),
) -> None:
    if repair_only:
        repair = True

    diff(agent=agent, project=project, save=True)

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

    preflight_exit_code = 0
    try:
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
) -> None:
    del yes
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

    result = run_repair(
        project_config,
        agent_state,
        kind=kind,
        dry_run=dry_run,
        allow_stash=True,
        active_branch_override=active_branch,
    )
    console.print(f"Repair log: {result.log_path}")
    if not result.success:
        print_error(result.message)
        raise typer.Exit(1)
    console.print(result.message)
    console.print(f"Next: cascade check {agent} --project {project}")


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
    summary_body = (
        "# Closeout Summary\n\n"
        f"- Project: {project}\n"
        f"- Agent: {agent}\n"
        f"- Issue: #{agent_state.get('issue', '')}\n"
        f"- Title: {agent_state.get('title', '')}\n"
        f"- Worktree: {worktree}\n"
        f"- Branch: {get_current_branch(worktree)}\n"
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
    table.add_row("Model recommended", "yes" if classification["model_recommended"] else "no")
    table.add_row("Suggested action", str(classification.get("suggested_no_model_action", "")))
    console.print(table)


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


@app.command()
def preflight(agent: str, project: str = typer.Option(...)) -> None:
    try:
        agent_state = load_agent_state(project, agent)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    project_file_value = agent_state.get("project_file")
    if not isinstance(project_file_value, str) or not project_file_value:
        print_error(
            "Agent state does not include project_file. Re-claim the issue or add project_file manually."
        )
        raise typer.Exit(1)

    try:
        project_config = load_project_config(Path(project_file_value))
    except ConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

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
    result = subprocess.run(
        preflight_command,
        cwd=worktree,
        shell=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    preflight_timestamp = timestamp_utc()
    log_content = result.stdout or ""
    log_path.write_text(
        (
            f"# Preflight Run\n"
            f"timestamp: {preflight_timestamp}\n"
            f"command: {preflight_command}\n"
            f"exit_code: {result.returncode}\n\n"
            f"{log_content}"
        ),
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
    if not passed:
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
    app()


if __name__ == "__main__":
    main()