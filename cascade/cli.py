from __future__ import annotations

import platform
import subprocess
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from cascade.config import ConfigError, ProjectConfig, instruction_file_paths, load_project_config
from cascade.commands import MODEL_BACKED_COMMANDS, NO_MODEL_COMMANDS, PLANNED_MODEL_BACKED_COMMANDS
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
from cascade.prompts import build_launch_prompt
from cascade.shell import CommandError, run_command
from cascade.state import (
    ensure_project_state_dirs,
    get_agent_run_dir,
    load_agent_state,
    save_agent_state,
    list_agent_states,
    update_agent_state,
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
        create_worktree_command = project.commands.create_worktree.format(
            agent=agent,
            slug=slug,
            branch=branch,
            issue=issue_number,
            project=project.name,
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


@app.command(name="run-agent", help="Launch interactive OpenCode session for this agent.")
def run_agent(
    agent: str,
    project: str = typer.Option(...),
    print_prompt: bool = typer.Option(False, "--print-prompt"),
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
    prompt_path = get_agent_run_dir(project, agent) / "launch_prompt.md"

    console.print(f"Worktree: {worktree}")
    console.print(f"Model: {model}")
    console.print(f"Prompt: {prompt_path}")
    if platform.system() == "Darwin":
        console.print(f"Clipboard: pbcopy < {prompt_path}")
    console.print(
        "Paste or load the generated launch prompt from "
        f"{prompt_path} after OpenCode starts."
    )

    if print_prompt:
        try:
            console.print(prompt_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            print_error(f"Launch prompt not found: {prompt_path}")
            raise typer.Exit(1) from exc

    try:
        ensure_opencode_available()
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1)

    result = subprocess.run(build_interactive_command(model), cwd=worktree, check=False)
    if result.returncode != 0:
        print_error(f"OpenCode exited with status {result.returncode}.")
        raise typer.Exit(result.returncode)


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
    table.add_column("Worktree")

    for item in states:
        table.add_row(
            str(item.get("agent", "")),
            str(item.get("issue", "")),
            str(item.get("slug", "")),
            str(item.get("engine", "")),
            str(item.get("model", "")),
            str(item.get("state", "")),
            str(item.get("worktree", "")),
        )
    console.print(table)


@app.command(name="show-prompt")
def show_prompt(agent: str, project: str = typer.Option(...)) -> None:
    prompt_path = get_agent_run_dir(project, agent) / "launch_prompt.md"
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
        updated = update_agent_state(project, agent, state.value)
    except (FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    console.print(f"Updated agent {agent} in project {project} to state '{updated['state']}'.")


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

    run_dir = get_agent_run_dir(project, agent)
    log_path = run_dir / "preflight.log"
    preflight_command = project_config.commands.preflight.format(
        agent=agent,
        slug=str(agent_state["slug"]),
        issue=str(agent_state["issue"]),
        project=project,
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
    log_path.write_text(
        (
            f"# Preflight Run\n"
            f"timestamp: {preflight_timestamp}\n"
            f"command: {preflight_command}\n"
            f"exit_code: {result.returncode}\n\n"
            f"{result.stdout}"
        ),
        encoding="utf-8",
    )

    new_state = (
        AgentLifecycleState.preflight_passed.value
        if result.returncode == 0
        else AgentLifecycleState.preflight_failed.value
    )
    updated_state = load_agent_state(project, agent)
    updated_state["state"] = new_state
    updated_state["preflight_last_run_at"] = preflight_timestamp
    updated_state["preflight_last_exit_code"] = result.returncode
    updated_state["preflight_last_log"] = str(log_path)
    save_agent_state(project, agent, updated_state)
    console.print(f"Preflight log: {log_path}")
    if result.returncode != 0:
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

    try:
        ensure_opencode_available()
    except OpenCodeError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    result = subprocess.run(build_interactive_command(str(agent_state["model"]), mode=mode), cwd=worktree, check=False)
    if result.returncode != 0:
        print_error(
            f"OpenCode exited with status {result.returncode}. If this appears flag-related, check `opencode --help`."
        )
        raise typer.Exit(result.returncode)

    agent_state["last_mode"] = mode.value if mode is not None else None
    agent_state["last_interaction_at"] = timestamp_utc()
    save_agent_state(project, agent, agent_state)


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