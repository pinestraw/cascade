from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


CONVERSATION_FILES: tuple[str, ...] = (
    "questions.md",
    "decisions.md",
    "running_summary.md",
    "transcript.md",
    "context.md",
    "diff.md",
    "opencode_session_id.txt",
    "continue_prompt.md",
    "preflight.log",
)


def ensure_conversation_files(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for filename in CONVERSATION_FILES:
        file_path = run_dir / filename
        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_markdown_entry(path: Path, heading: str, body: str) -> None:
    entry = f"## {heading}\n\n{body.strip()}\n\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def read_tail_chars(path: Path, max_chars: int) -> str:
    content = read_text(path)
    if len(content) <= max_chars:
        return content
    return content[-max_chars:]


def build_ask_prompt(
    question: str,
    issue: int,
    title: str,
    running_summary: str,
    decisions: str,
) -> str:
    return (
        f"You are working on GitHub issue #{issue}: {title}.\n\n"
        "User follow-up question:\n"
        f"{question.strip()}\n\n"
        "Current running summary:\n"
        f"{running_summary or '(none)'}\n\n"
        "Recorded decisions/clarifications:\n"
        f"{decisions or '(none)'}\n\n"
        "Answer directly. If blocked, list exactly what you need from the user."
    )


def build_continue_prompt(
    issue: int,
    title: str,
    mandate: str,
    running_summary: str,
    decisions: str,
    questions: str,
    preflight_log: str,
) -> str:
    return (
        f"Continue work on GitHub issue #{issue}: {title}.\n\n"
        "Mandate:\n"
        f"{mandate or '(none)'}\n\n"
        "Running summary:\n"
        f"{running_summary or '(none)'}\n\n"
        "Decisions:\n"
        f"{decisions or '(none)'}\n\n"
        "Open questions:\n"
        f"{questions or '(none)'}\n\n"
        "Latest preflight log excerpt:\n"
        f"{preflight_log or '(none)'}\n\n"
        "Reconfirm plan, list remaining blockers, and proceed safely according to repo rules."
    )


def build_summarize_prompt(
    issue: int,
    title: str,
    mandate: str,
    git_status: str,
    transcript_excerpt: str,
    decisions: str,
) -> str:
    return (
        f"Summarize the current state for GitHub issue #{issue}: {title}.\n\n"
        "Mandate:\n"
        f"{mandate or '(none)'}\n\n"
        "Git status --short:\n"
        f"{git_status or '(clean)'}\n\n"
        "Recent transcript excerpt:\n"
        f"{transcript_excerpt or '(none)'}\n\n"
        "Decisions:\n"
        f"{decisions or '(none)'}\n\n"
        "Produce a concise running summary with completed work, in-progress work, blockers, and next actions."
    )
