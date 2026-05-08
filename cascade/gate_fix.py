"""Headless OpenRouter-based gate-fix loop for model-fixable gate failures.

This module implements a deterministic, non-interactive fix loop that:
- classifies whether a gate failure is safe to route to a model
- calls OpenRouter with streaming enabled and prints live terminal output
- applies only deterministic, fail-closed edits inside the assigned worktree
- reruns the exact failed gate command after each attempt
- records full artifacts for prompts, streams, reruns, and final summary
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import requests

from cascade.config import ModelProfile
from cascade.costs import estimate_cost, estimate_tokens
from cascade.gates import classify_gate_failure, failure_signature as build_failure_signature
from cascade.mandate_meta import read_mandate_metadata
from cascade.shell import CommandError, run_command


_FILE_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:py|pyi|sh|js|jsx|ts|tsx|json|ya?ml|toml|ini|cfg|md|txt))(?::\d+(?::\d+)?)?"
)
_STRUCTURED_JSON_RE = re.compile(r"```json\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)
_DIFF_BLOCK_RE = re.compile(r"```diff\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)


class GateFixCategory(str, Enum):
    """Categories of gate failures and whether they should use the model."""

    METADATA = "metadata"
    BRANCH_MISMATCH = "branch_mismatch"
    DOCKER_RUNTIME = "docker_runtime"
    WORKFLOW = "workflow"
    DOCSTRING = "docstring"
    LINTING = "linting"
    TYPING = "typing"
    FORMATTING = "formatting"
    IMPORTS = "imports"
    SMALL_TEST = "small_test"
    SERIALIZER = "serializer"


class GateFixBatchMode(str, Enum):
    FILE = "file"
    GROUP = "group"
    BROAD = "broad"


@dataclass
class GateFixConfig:
    """Configuration for a single gate-fix run."""

    model: str
    max_attempts: int = 3
    max_estimated_cost_usd: float = 0.25
    stream: bool = True
    debug: bool = False
    fallback_models: list[str] | None = None
    expected_output_tokens: int = 12000
    batch_mode: GateFixBatchMode = GateFixBatchMode.FILE

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "max_attempts": self.max_attempts,
            "max_estimated_cost_usd": self.max_estimated_cost_usd,
            "stream": self.stream,
            "debug": self.debug,
            "fallback_models": list(self.fallback_models or []),
            "expected_output_tokens": self.expected_output_tokens,
            "batch_mode": self.batch_mode.value,
        }


@dataclass
class GateFixAttempt:
    """Metadata for one model attempt."""

    attempt_number: int
    model: str
    prompt_tokens: int
    expected_output_tokens: int
    estimated_cost: float
    request_metadata: dict[str, object]
    response_metadata: dict[str, object] = field(default_factory=dict)
    response_summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    success: bool = False
    failure_signature: str | None = None
    failure_reason: str | None = None
    streamed_output: str = ""
    patch_apply_result: str = ""
    rerun_command: str = ""
    rerun_result: str = ""
    diff_size_after: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_number": self.attempt_number,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "expected_output_tokens": self.expected_output_tokens,
            "estimated_cost_usd": self.estimated_cost,
            "request_metadata": self.request_metadata,
            "response_metadata": self.response_metadata,
            "response_summary": self.response_summary,
            "changed_files": self.changed_files,
            "success": self.success,
            "failure_signature": self.failure_signature,
            "failure_reason": self.failure_reason,
            "patch_apply_result": self.patch_apply_result,
            "rerun_command": self.rerun_command,
            "rerun_result": "(omitted - see gate_fix_attempt_N.rerun.log)",
            "diff_size_after": self.diff_size_after,
        }


@dataclass
class GateFixResult:
    """Final result of a gate-fix run."""

    success: bool
    attempts: list[GateFixAttempt]
    total_estimated_cost: float
    stop_reason: str
    initial_model: str = ""
    fallback_chain: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "initial_model": self.initial_model,
            "fallback_chain": self.fallback_chain,
            "attempts_count": len(self.attempts),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "total_estimated_cost": f"${self.total_estimated_cost:.4f}",
            "total_estimated_cost_usd": self.total_estimated_cost,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "failure_signatures": [attempt.failure_signature for attempt in self.attempts if attempt.failure_signature],
            "changed_files_by_attempt": [attempt.changed_files for attempt in self.attempts],
        }


@dataclass(frozen=True)
class StructuredEdit:
    path: str
    old_text: str | None = None
    new_text: str | None = None
    content: str | None = None


def classify_failure_as_model_fixable(log_text: str, hook: str | None) -> GateFixCategory:
    """Classify a gate failure as model-fixable or deterministic."""
    if not log_text.strip():
        return GateFixCategory.WORKFLOW

    classification = classify_gate_failure(log_text)
    gate_category = str(classification.get("category") or "unknown")
    hook_name = str(hook or classification.get("hook") or "").lower()
    log_lower = log_text.lower()

    if "branch mismatch" in log_lower or "branch" in hook_name:
        return GateFixCategory.BRANCH_MISMATCH
    if "metadata" in hook_name or "mandate" in log_lower:
        return GateFixCategory.WORKFLOW

    if gate_category in {"workflow", "environment", "security", "policy", "migration"}:
        if "docker" in hook_name or "network" in log_lower or "daemon" in log_lower:
            return GateFixCategory.DOCKER_RUNTIME
        return GateFixCategory.WORKFLOW

    if gate_category == "typing":
        return GateFixCategory.TYPING

    if gate_category == "linting":
        if any(token in hook_name or token in log_lower for token in ("format", "black", "ruff-format", "trailing-whitespace", "end-of-file")):
            return GateFixCategory.FORMATTING
        if any(token in hook_name or token in log_lower for token in ("import", "isort", "unused import")):
            return GateFixCategory.IMPORTS
        return GateFixCategory.LINTING

    if gate_category == "coverage":
        if any(token in hook_name or token in log_lower for token in ("docstring", "d100", "d101", "d102", "d103", "d104")):
            return GateFixCategory.DOCSTRING
        return GateFixCategory.SMALL_TEST

    if any(token in hook_name or token in log_lower for token in ("docstring", "d100", "d101", "d102", "d103", "d104")):
        return GateFixCategory.DOCSTRING
    if any(token in hook_name or token in log_lower for token in ("ruff", "flake8", "lint", "style", "hadolint", "actionlint", "shellcheck")):
        return GateFixCategory.LINTING
    if any(token in hook_name or token in log_lower for token in ("pyright", "mypy", "type", "typing", "annotation")):
        return GateFixCategory.TYPING
    if any(token in hook_name or token in log_lower for token in ("format", "black", "ruff-format", "whitespace")):
        return GateFixCategory.FORMATTING
    if any(token in hook_name or token in log_lower for token in ("import", "isort", "unused import")):
        return GateFixCategory.IMPORTS
    if any(token in hook_name or token in log_lower for token in ("serializer", "viewset", "response schema", "api response")):
        return GateFixCategory.SERIALIZER
    if any(token in hook_name or token in log_lower for token in ("test", "assert", "pytest", "unittest", "expected", "actual")):
        return GateFixCategory.SMALL_TEST

    return GateFixCategory.WORKFLOW


def is_model_fixable(category: GateFixCategory) -> bool:
    model_fixable = {
        GateFixCategory.DOCSTRING,
        GateFixCategory.LINTING,
        GateFixCategory.TYPING,
        GateFixCategory.FORMATTING,
        GateFixCategory.IMPORTS,
        GateFixCategory.SMALL_TEST,
        GateFixCategory.SERIALIZER,
    }
    return category in model_fixable


def _looks_like_scratch_path(path: str) -> bool:
    lower = path.lower()
    name = Path(path).name.lower()
    return any(
        token in lower
        for token in ("debug", "scratch", ".bak", ".tmp", ".swp", "__pycache__", ".coverage")
    ) or name.endswith("~")


def _validate_relative_path(worktree: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError(f"Invalid file path: {relative_path!r}")

    normalized = relative_path
    if normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]

    full_path = (worktree / normalized).resolve()
    try:
        full_path.relative_to(worktree.resolve())
    except ValueError as exc:
        raise ValueError(f"File outside worktree: {relative_path}") from exc
    return full_path


def _git_status_lines(worktree: Path) -> list[str]:
    try:
        output = run_command("git status --porcelain", cwd=worktree).stdout.strip()
    except CommandError:
        return []
    return [line for line in output.splitlines() if line.strip()]


def get_current_dirty_files(worktree: Path) -> list[str]:
    dirty_files: list[str] = []
    for line in _git_status_lines(worktree):
        if len(line) >= 4:
            dirty_files.append(line[3:].strip())
    return dirty_files


def _get_status_summary(worktree: Path, *, max_lines: int = 30) -> list[str]:
    return _git_status_lines(worktree)[:max_lines]


def _get_diff_size(worktree: Path) -> int:
    try:
        output = run_command("git diff --numstat HEAD", cwd=worktree).stdout.strip()
    except CommandError:
        return 0

    total = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed = parts[0], parts[1]
        if added.isdigit():
            total += int(added)
        if removed.isdigit():
            total += int(removed)
    return total


def _is_tracked_file(worktree: Path, relative_path: str) -> bool:
    try:
        run_command(f"git ls-files --error-unmatch {shlex.quote(relative_path)}", cwd=worktree)
        return True
    except CommandError:
        return False


def _is_commit_gate_command(command: str) -> bool:
    normalized = command.strip().lower()
    return "git commit" in normalized


def _stage_gate_fix_files_for_commit(
    worktree: Path,
    changed_files: list[str],
    dirty_or_staged_before: set[str],
) -> tuple[bool, str]:
    if not changed_files:
        return True, "No model-changed files needed staging"

    eligible_files: list[str] = []
    seen: set[str] = set()
    for path in changed_files:
        if path in seen:
            continue
        seen.add(path)

        if _looks_like_scratch_path(path):
            return False, f"Refusing suspicious scratch/debug path: {path}"

        try:
            _validate_relative_path(worktree, path)
        except ValueError as exc:
            return False, str(exc)

        if path in dirty_or_staged_before or _is_tracked_file(worktree, path):
            eligible_files.append(path)

    if not eligible_files:
        return True, "No model-changed files met safe staging rules"

    quoted = " ".join(shlex.quote(path) for path in eligible_files)
    try:
        run_command(f"git add -- {quoted}", cwd=worktree)
    except CommandError as exc:
        message = exc.output.strip() or str(exc)
        return False, message

    return True, f"Staging model-changed files for commit gate: {', '.join(eligible_files)}"


def _extract_log_file_paths(log_text: str) -> list[str]:
    matches: list[str] = []
    for match in _FILE_PATH_RE.finditer(log_text):
        matches.append(match.group("path"))
    return matches


def _extract_log_file_frequencies(log_text: str) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for match in _FILE_PATH_RE.finditer(log_text):
        path = match.group("path")
        frequencies[path] = frequencies.get(path, 0) + 1
    return frequencies


def _prefers_small_batch(category: GateFixCategory) -> bool:
    return category in {
        GateFixCategory.TYPING,
        GateFixCategory.LINTING,
        GateFixCategory.DOCSTRING,
        GateFixCategory.SMALL_TEST,
    }


def _select_dominant_failure_file(
    *,
    log_text: str,
    candidate_files: list[str],
    touched_files: list[str],
    dirty_files: list[str],
) -> tuple[str | None, bool, dict[str, int]]:
    if not candidate_files:
        return None, False, {}

    frequencies = _extract_log_file_frequencies(log_text)
    scores: dict[str, int] = {}
    for path in candidate_files:
        score = frequencies.get(path, 0)
        if path in touched_files:
            score += 2
        if path in dirty_files:
            score += 1
        scores[path] = score

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return candidate_files[0], False, scores

    top_path, top_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0
    dominates = top_score >= 2 and (top_score - second_score >= 2)
    return top_path, dominates, scores


def _select_batch_files(
    *,
    mode: GateFixBatchMode,
    category: GateFixCategory,
    log_text: str,
    touched_files: list[str],
    dirty_files: list[str],
    candidate_files: list[str],
    forced_file: str | None,
) -> tuple[list[str], str, str | None, bool]:
    if forced_file and forced_file in candidate_files:
        return [forced_file], "forced-single-file-retry", forced_file, True

    if mode == GateFixBatchMode.BROAD:
        return candidate_files, "broad-batch", None, False

    dominant_file, dominates, _scores = _select_dominant_failure_file(
        log_text=log_text,
        candidate_files=candidate_files,
        touched_files=touched_files,
        dirty_files=dirty_files,
    )

    if mode == GateFixBatchMode.FILE:
        if _prefers_small_batch(category) and dominant_file is not None:
            return [dominant_file], "file-local-dominant", dominant_file, dominates
        if dominant_file is not None:
            return [dominant_file], "file-local-default", dominant_file, dominates
        return candidate_files[:1], "file-local-fallback", None, False

    # GROUP mode: small, related set around dominant file.
    if dominant_file is None:
        return candidate_files[: min(3, len(candidate_files))], "group-fallback", None, False

    dominant_dir = str(Path(dominant_file).parent)
    grouped = [dominant_file]
    for path in candidate_files:
        if path == dominant_file:
            continue
        if str(Path(path).parent) == dominant_dir:
            grouped.append(path)
        if len(grouped) >= 3:
            break
    if len(grouped) == 1:
        for path in candidate_files:
            if path == dominant_file:
                continue
            grouped.append(path)
            if len(grouped) >= 3:
                break
    return grouped, "group-local", dominant_file, dominates


def _select_patch_mode_preference(
    *,
    target_files: list[str],
    force_full_file_for: str | None,
    diff_size: int,
) -> str:
    if force_full_file_for and force_full_file_for in target_files:
        return "full_file"
    if len(target_files) != 1:
        return "anchored_edits"
    del diff_size
    return "anchored_edits"


def _select_candidate_files(
    *,
    worktree: Path,
    log_text: str,
    touched_files: list[str],
    dirty_files: list[str],
    max_files: int = 6,
) -> list[str]:
    ordered_candidates: list[str] = []
    for path in _extract_log_file_paths(log_text) + touched_files + dirty_files:
        if path in ordered_candidates:
            continue
        try:
            full_path = _validate_relative_path(worktree, path)
        except ValueError:
            continue
        if full_path.exists() and full_path.is_file():
            ordered_candidates.append(path)
        if len(ordered_candidates) >= max_files:
            break
    return ordered_candidates


def _extract_rerun_support_files(
    *,
    rerun_output: str,
    worktree: Path,
    current_target_files: list[str],
    max_support_files: int = 2,
) -> list[str]:
    """Extract files explicitly referenced in rerun output that are not already in the target batch.

    Used to auto-expand the next attempt's context when a rerun failure implicates new files
    (e.g. a stale complexity baseline entry referencing config/complexity/c901-baseline.txt).
    """
    current_set = set(current_target_files)
    support_files: list[str] = []
    for path in _extract_log_file_paths(rerun_output):
        if path in current_set:
            continue
        try:
            full = _validate_relative_path(worktree, path)
        except ValueError:
            continue
        if not full.exists() or not full.is_file():
            continue
        support_files.append(path)
        if len(support_files) >= max_support_files:
            break
    return support_files


def _read_candidate_file_contexts(
    worktree: Path,
    candidate_files: list[str],
) -> dict[str, str]:
    contexts: dict[str, str] = {}

    for path in candidate_files:
        full_path = _validate_relative_path(worktree, path)
        try:
            content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        contexts[path] = content

    return contexts


def build_gate_fix_prompt(
    *,
    project_name: str,
    agent: str,
    worktree: Path,
    branch: str,
    mandate_slug: str,
    failing_command: str,
    failing_hook: str,
    failing_log: str,
    dirty_files: list[str],
    changed_files: list[str],
    mandate_scope: list[str] | None = None,
    status_summary: list[str] | None = None,
    file_contexts: dict[str, str] | None = None,
    target_files: list[str] | None = None,
    batch_mode: GateFixBatchMode = GateFixBatchMode.FILE,
    dominant_file: str | None = None,
    patch_mode_preference: str = "anchored_edits",
) -> str:
    dirty_summary = "\n".join(f"- {item}" for item in (status_summary or dirty_files)[:30]) or "- (none)"
    changed_summary = "\n".join(f"- {item}" for item in changed_files[:30]) or "- (none)"
    scope_summary = "\n".join(f"- {item}" for item in (mandate_scope or [])[:30]) or "- (not available)"
    batch_summary = "\n".join(f"- {item}" for item in (target_files or [])[:30]) or "- (none resolved)"
    log_tail = "\n".join(failing_log.splitlines()[-80:])

    file_context_sections: list[str] = []
    for path, content in (file_contexts or {}).items():
        suffix = Path(path).suffix.lstrip(".") or "text"
        file_context_sections.append(f"### {path}\n```{suffix}\n{content}\n```")
    file_context_block = "\n\n".join(file_context_sections) or "(no candidate file contents available)"

    return f"""You are a deterministic code-fix agent working inside Cascade.

Repo/project: {project_name}
Agent: {agent}
Worktree path: {worktree}
Branch: {branch}
Mandate slug: {mandate_slug}

Exact failing command:
{failing_command}

Exact failing hook/check:
{failing_hook}

Exact failing output:
{log_tail}

Dirty and staged files summary:
{dirty_summary}

Changed files summary:
{changed_summary}

Mandate scope (if available):
{scope_summary}

Relevant file contents:
{file_context_block}

Current batch mode: {batch_mode.value}
Dominant failure file: {dominant_file or '(none)'}
Target file batch for this attempt:
{batch_summary}
Patch mode preference for this attempt: {patch_mode_preference}

Requirements:
- Modify only files needed to fix the current gate failure.
- Treat this attempt as a small local batch. Prefer changing only target batch files above.
- Do not change branch, metadata, or workflow state.
- Do not create scratch or debug files.
- Do not edit unrelated files.
- Do not weaken tests, gates, or policy.
- Do not bypass checks.
- Stop once the specific failing gate is fixed.
- Prefer the smallest direct fix.
- If patch mode preference is full_file and one target file is provided, return exactly one full-file replacement edit for that file.
- If patch mode preference is anchored_edits, each old_text must be uniquely anchored and must match exactly once. If exact uniqueness is not possible, return one full-file replacement for that file instead.

Return ONLY one of these safe formats:

1. Preferred JSON with deterministic edits:
```json
{{
  "summary": "brief human summary",
  "edits": [
    {{
      "path": "relative/path.py",
      "old_text": "exact existing text",
      "new_text": "replacement text"
    }}
  ]
}}
```

2. Full-file JSON replacement when exact search/replace is impractical:
```json
{{
  "summary": "brief human summary",
  "edits": [
    {{
      "path": "relative/path.py",
      "content": "full new file content"
    }}
  ]
}}
```

3. If necessary, a unified diff inside ```diff``` fences.

If you cannot produce a safe deterministic fix, return JSON with an empty edits list and an `unable_to_fix` reason.
"""


def extract_json_from_response(response_text: str) -> dict[str, Any] | None:
    match = _STRUCTURED_JSON_RE.search(response_text)
    if match is not None:
        try:
            payload = json.loads(match.group("body"))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload

    stripped = response_text.strip()
    if stripped:
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload

    first = response_text.find("{")
    last = response_text.rfind("}")
    if first != -1 and last != -1 and first < last:
        try:
            payload = json.loads(response_text[first : last + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def _extract_diff_from_response(response_text: str) -> str | None:
    match = _DIFF_BLOCK_RE.search(response_text)
    if match is not None:
        diff_text = match.group("body").strip()
        return diff_text or None

    if "diff --git" in response_text or ("--- " in response_text and "+++ " in response_text and "@@" in response_text):
        return response_text.strip()
    return None


def _parse_structured_edits(payload: dict[str, Any]) -> tuple[list[StructuredEdit], str, str | None]:
    summary = str(payload.get("summary") or payload.get("explanation") or "")
    unable = payload.get("unable_to_fix")
    if unable is not None:
        return [], summary, str(unable)

    raw_edits = payload.get("edits")
    if raw_edits is None:
        if payload.get("fixed") is False:
            return [], summary, str(payload.get("reason") or "Model could not fix the failure")
        return [], summary, "Model response did not contain deterministic edits."

    if not isinstance(raw_edits, list):
        return [], summary, "Model response contained non-list edits."

    edits: list[StructuredEdit] = []
    for item in raw_edits:
        if not isinstance(item, dict):
            return [], summary, "Model edit entry was not an object."
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            return [], summary, "Model edit entry is missing a valid path."
        content = item.get("content")
        old_text = item.get("old_text")
        new_text = item.get("new_text")
        if isinstance(content, str):
            edits.append(StructuredEdit(path=path.strip(), content=content))
            continue
        if isinstance(old_text, str) and isinstance(new_text, str):
            edits.append(StructuredEdit(path=path.strip(), old_text=old_text, new_text=new_text))
            continue
        return [], summary, f"Edit for {path} is missing either content or old_text/new_text."

    if not edits:
        return [], summary, "Model returned no edits."
    return edits, summary, None


def _apply_structured_edits(worktree: Path, edits: list[StructuredEdit]) -> tuple[bool, list[str], str]:
    changed_files: list[str] = []
    for edit in edits:
        if _looks_like_scratch_path(edit.path):
            return False, changed_files, f"Refusing suspicious scratch/debug path: {edit.path}"
        full_path = _validate_relative_path(worktree, edit.path)

        if edit.content is not None:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            current_content = ""
            if full_path.exists():
                try:
                    current_content = full_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return False, changed_files, f"Failed to read {edit.path}: {exc}"
            if current_content != edit.content:
                full_path.write_text(edit.content, encoding="utf-8")
                changed_files.append(edit.path)
            continue

        if edit.old_text is None or edit.new_text is None:
            return False, changed_files, f"Structured edit for {edit.path} is incomplete."
        if not full_path.exists():
            return False, changed_files, f"File does not exist for search/replace edit: {edit.path}"
        try:
            current = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return False, changed_files, f"Failed to read {edit.path}: {exc}"

        occurrences = current.count(edit.old_text)
        if occurrences != 1:
            return False, changed_files, f"Search text for {edit.path} matched {occurrences} times; refusing ambiguous patch."

        updated = current.replace(edit.old_text, edit.new_text, 1)
        if updated != current:
            full_path.write_text(updated, encoding="utf-8")
            changed_files.append(edit.path)

    return True, changed_files, "Applied structured deterministic edits."


def _diff_paths(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            _, raw_path = line.split(" ", 1)
            raw_path = raw_path.strip()
            if raw_path == "/dev/null":
                continue
            if raw_path.startswith("a/") or raw_path.startswith("b/"):
                raw_path = raw_path[2:]
            if raw_path not in paths:
                paths.append(raw_path)
    return paths


def _apply_unified_diff(worktree: Path, diff_text: str) -> tuple[bool, list[str], str]:
    if not diff_text.strip():
        return False, [], "Empty diff output."

    changed_files = _diff_paths(diff_text)
    if not changed_files:
        return False, [], "Diff did not identify any files."
    for path in changed_files:
        if _looks_like_scratch_path(path):
            return False, [], f"Refusing suspicious scratch/debug path in diff: {path}"
        _validate_relative_path(worktree, path)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as handle:
            handle.write(diff_text)
            temp_path = Path(handle.name)

        run_command(
            f"git apply --check --recount --whitespace=nowarn {shlex.quote(str(temp_path))}",
            cwd=worktree,
        )
        run_command(
            f"git apply --recount --whitespace=nowarn {shlex.quote(str(temp_path))}",
            cwd=worktree,
        )
    except CommandError as exc:
        return False, [], f"Unified diff could not be applied cleanly: {exc.output or exc}"
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return True, changed_files, "Applied unified diff cleanly."


def apply_model_fixes(worktree: Path, model_response: str) -> tuple[bool, list[str], str]:
    payload = extract_json_from_response(model_response)
    if payload is not None:
        edits, _summary, error = _parse_structured_edits(payload)
        if error is None:
            return _apply_structured_edits(worktree, edits)
        diff_text = _extract_diff_from_response(model_response)
        if diff_text is None:
            return False, [], error

    diff_text = _extract_diff_from_response(model_response)
    if diff_text is not None:
        return _apply_unified_diff(worktree, diff_text)
    return False, [], "Model response did not contain safe structured edits or a clean unified diff."


def detect_unrelated_file_growth(
    original_files: set[str],
    current_files: set[str],
    expected_fixes: list[str],
) -> bool:
    new_files = current_files - original_files
    expected_set = set(expected_fixes)
    unrelated_new = new_files - expected_set
    if len(unrelated_new) > 5:
        return True
    if expected_fixes and len(unrelated_new) > max(2, len(expected_fixes) // 2):
        return True
    return False


def run_gate_recheck(
    worktree: Path,
    gate_command: str,
    run_dir: Path,
    attempt_number: int,
) -> tuple[bool, str]:
    log_file = run_dir / f"gate_fix_attempt_{attempt_number}.rerun.log"
    try:
        result = run_command(gate_command, cwd=worktree)
        output = result.stdout
        log_file.write_text(output, encoding="utf-8")
        return True, output
    except CommandError as exc:
        output = exc.output
        log_file.write_text(output, encoding="utf-8")
        return False, output


def _build_probe_command(gate_command: str, failure_source: str | None) -> str:
    command = gate_command.strip()
    if not _is_commit_gate_command(command):
        return command

    # Preserve commit-hook semantics by default; callers may pass a precomputed
    # no-op-safe equivalent in gate_command if their workflow supports it.
    del failure_source
    return command


def _git_output_or_empty(worktree: Path, command: str) -> str:
    try:
        return run_command(command, cwd=worktree).stdout
    except CommandError:
        return ""


def _compute_failure_context_hash(
    *,
    worktree: Path,
    failing_command: str,
    failure_signature: str,
) -> str:
    head = _git_output_or_empty(worktree, "git rev-parse HEAD").strip()
    staged_diff = _git_output_or_empty(worktree, "git diff --cached --no-color")
    unstaged_diff = _git_output_or_empty(worktree, "git diff --no-color")
    payload = {
        "head": head,
        "staged_diff_hash": hashlib.sha256(staged_diff.encode("utf-8")).hexdigest(),
        "unstaged_diff_hash": hashlib.sha256(unstaged_diff.encode("utf-8")).hexdigest(),
        "failing_command": failing_command,
        "failure_signature": failure_signature,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _run_current_gate_probe(
    *,
    worktree: Path,
    run_dir: Path,
    gate_command: str,
    failing_hook: str,
    fallback_category: GateFixCategory,
    gate_result: dict[str, object] | None,
    failure_source: str | None,
) -> tuple[bool, str, str, GateFixCategory, bool, str, str]:
    probe_command = _build_probe_command(gate_command, failure_source)
    print("[probe] running current gate probe")
    passed, probe_output = run_gate_recheck(worktree, probe_command, run_dir, 0)

    classification = classify_gate_failure(probe_output)
    hook = str(classification.get("hook") or failing_hook or "unknown")
    category = classify_failure_as_model_fixable(probe_output, hook)
    if passed and not probe_output.strip():
        category = fallback_category
    model_fixable = is_model_fixable(category)
    signature = "passed" if passed else (build_failure_signature(gate_result, probe_output) or "")

    print(f"[probe] hook/check: {hook}")
    print(f"[probe] category: {category.value}")
    print(f"[probe] model-fixable: {'yes' if model_fixable else 'no'}")
    return passed, probe_output, hook, category, model_fixable, signature, probe_command


def _extract_stale_complexity_baseline_entries(rerun_output: str) -> list[str]:
    baseline_path = "config/complexity/c901-baseline.txt"
    entries: list[str] = []
    for line in rerun_output.splitlines():
        lower = line.lower()
        if baseline_path not in line or "stale entry" not in lower:
            continue
        for match in re.finditer(r"([A-Za-z0-9_.\-/]+\|[A-Za-z0-9_.\-]+)", line):
            entry = match.group(1)
            if entry not in entries:
                entries.append(entry)
    return entries


def _remove_stale_complexity_baseline_entries(
    *,
    worktree: Path,
    entries: list[str],
) -> tuple[bool, list[str], str]:
    if not entries:
        return False, [], "No stale complexity baseline entries detected"

    baseline_path = "config/complexity/c901-baseline.txt"
    try:
        full = _validate_relative_path(worktree, baseline_path)
    except ValueError:
        return False, [], "Invalid complexity baseline path"
    if not full.exists() or not full.is_file():
        return False, [], "Complexity baseline file not found"

    try:
        original_lines = full.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return False, [], f"Failed to read complexity baseline file: {exc}"

    kept_lines: list[str] = []
    removed: list[str] = []
    for line in original_lines:
        stripped = line.strip()
        should_remove = False
        for entry in entries:
            if stripped.startswith(f"{entry}:") or stripped == entry or entry in stripped:
                should_remove = True
                removed.append(entry)
                break
        if not should_remove:
            kept_lines.append(line)

    if len(kept_lines) == len(original_lines):
        return False, [], "No matching stale baseline lines found"

    new_content = "\n".join(kept_lines)
    if kept_lines:
        new_content += "\n"
    full.write_text(new_content, encoding="utf-8")
    removed_unique = list(dict.fromkeys(removed))
    return True, [baseline_path], ", ".join(removed_unique)


def _extract_safe_formatting_command(rerun_output: str) -> str | None:
    candidates: list[str] = []

    for command in re.findall(r"`([^`]+)`", rerun_output):
        candidates.append(command.strip())
    for line in rerun_output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ruff format", "ruff check --fix", "black ", "isort ")):
            candidates.append(stripped)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parts = shlex.split(candidate)
        except ValueError:
            continue
        if not parts:
            continue
        if any(token in {";", "&&", "||", "|"} for token in parts):
            continue

        if parts[:2] == ["ruff", "format"]:
            return " ".join(shlex.quote(part) for part in parts)
        if parts[:3] == ["ruff", "check", "--fix"]:
            return " ".join(shlex.quote(part) for part in parts)
        if parts[0] in {"black", "isort"}:
            return " ".join(shlex.quote(part) for part in parts)

    return None


def _build_fallback_formatting_command(*, category: GateFixCategory, target_files: list[str]) -> str | None:
    if category != GateFixCategory.FORMATTING:
        return None
    if not target_files:
        return None
    quoted = " ".join(shlex.quote(path) for path in target_files)
    return f"ruff format {quoted}"


def _run_deterministic_formatter(
    *,
    worktree: Path,
    command: str,
    dirty_before: set[str],
) -> tuple[bool, list[str], str]:
    try:
        run_command(command, cwd=worktree)
    except CommandError as exc:
        message = exc.output.strip() or str(exc)
        return False, [], message

    dirty_after = set(get_current_dirty_files(worktree))
    changed = sorted(dirty_after - dirty_before)
    if not changed:
        changed = sorted(dirty_after)
    return True, changed, command


def _deterministic_repair_guidance(
    *,
    rerun_output: str,
    project_name: str,
    agent: str,
) -> str:
    classification = classify_gate_failure(rerun_output)
    action = str(classification.get("suggested_no_model_action") or "")
    if not action.strip():
        return "Run `cascade repair <agent> --project <project>` or inspect the rerun log for deterministic repair."
    return action.replace("<agent>", agent).replace("<project>", project_name)


def _save_attempt_failure_context(
    *,
    run_dir: Path,
    attempt_number: int,
    gate_command: str,
    rerun_hook: str,
    rerun_category: GateFixCategory,
    model_fixable: bool,
    rerun_signature: str | None,
    rerun_output: str,
) -> None:
    payload = {
        "attempt": attempt_number,
        "source": "gate-fix-rerun",
        "command": gate_command,
        "hook": rerun_hook,
        "category": rerun_category.value,
        "model_fixable": model_fixable,
        "failure_signature": rerun_signature,
        "log": rerun_output,
        "log_file": f"gate_fix_attempt_{attempt_number}.rerun.log",
    }
    (run_dir / f"gate_fix_attempt_{attempt_number}.failure_context.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "gate_fix_latest_failure_context.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def check_branch_drift(worktree: Path, expected_branch: str) -> bool:
    try:
        current = run_command("git rev-parse --abbrev-ref HEAD", cwd=worktree).stdout.strip()
    except CommandError:
        return True
    return current != expected_branch


def _append_prompt_artifact(run_dir: Path, attempt_number: int, prompt: str) -> None:
    latest_path = run_dir / "gate_fix_prompt.md"
    attempt_path = run_dir / f"gate_fix_attempt_{attempt_number}.prompt.md"
    attempt_path.write_text(prompt, encoding="utf-8")

    section = f"# Attempt {attempt_number}\n\n{prompt}\n"
    if latest_path.exists():
        latest_path.write_text(latest_path.read_text(encoding="utf-8") + "\n\n---\n\n" + section, encoding="utf-8")
    else:
        latest_path.write_text(section, encoding="utf-8")


def _write_attempt_summary(run_dir: Path, attempt: GateFixAttempt) -> None:
    summary_path = run_dir / f"gate_fix_attempt_{attempt.attempt_number}.summary.log"
    lines = [
        f"attempt: {attempt.attempt_number}",
        f"model: {attempt.model}",
        f"prompt_tokens: {attempt.prompt_tokens}",
        f"expected_output_tokens: {attempt.expected_output_tokens}",
        f"estimated_cost_usd: {attempt.estimated_cost:.6f}",
        f"success: {attempt.success}",
        f"response_summary: {attempt.response_summary}",
        f"patch_apply_result: {attempt.patch_apply_result}",
        f"failure_signature: {attempt.failure_signature or ''}",
        f"failure_reason: {attempt.failure_reason or ''}",
        f"rerun_command: {attempt.rerun_command}",
        f"changed_files: {', '.join(attempt.changed_files) if attempt.changed_files else '(none)'}",
    ]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_model_call_artifact(
    run_dir: Path,
    *,
    selected_model: str,
    fallback_models: list[str],
    attempt: GateFixAttempt,
) -> None:
    artifact_path = run_dir / "gate_fix_model_call.json"
    payload: dict[str, Any]
    if artifact_path.exists():
        try:
            existing = json.loads(artifact_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        payload = existing if isinstance(existing, dict) else {}
    else:
        payload = {}

    calls = payload.get("calls")
    if not isinstance(calls, list):
        calls = []

    calls.append(
        {
            "attempt": attempt.attempt_number,
            "model": attempt.model,
            "prompt_tokens": attempt.prompt_tokens,
            "expected_output_tokens": attempt.expected_output_tokens,
            "estimated_cost_usd": attempt.estimated_cost,
            "request_metadata": attempt.request_metadata,
            "response_metadata": attempt.response_metadata,
        }
    )

    payload.update(
        {
            "selected_model": selected_model,
            "fallback_models": fallback_models,
            "calls": calls,
        }
    )
    artifact_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _emit_model_line(text: str) -> None:
    print(f"[model] {text}", flush=True)


def _has_structured_boundary(text: str) -> bool:
    return "```" in text or text.endswith("\n\n")


def _flush_model_stream_buffer(
    buffer: str,
    *,
    force: bool,
    flush_threshold: int,
) -> str:
    pending = buffer

    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        if line:
            _emit_model_line(line)

    if not force and pending and _has_structured_boundary(pending):
        _emit_model_line(pending)
        return ""

    if not force and len(pending) >= flush_threshold:
        split_at = max(pending.rfind(" "), pending.rfind("\t"))
        if split_at <= 0:
            split_at = flush_threshold
        chunk = pending[:split_at].rstrip()
        pending = pending[split_at:]
        if chunk:
            _emit_model_line(chunk)
        pending = pending.lstrip()

    if force and pending.strip():
        _emit_model_line(pending.strip())
        pending = ""

    return pending


def stream_openrouter_request(
    model: str,
    messages: list[dict[str, str]],
    config: GateFixConfig,
    run_dir: Path,
    attempt_number: int,
) -> tuple[str, dict[str, object], dict[str, object]]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY environment variable not set")

    url = "https://openrouter.ai/api/v1/chat/completions"
    request_metadata: dict[str, object] = {
        "url": url,
        "model": model,
        "stream": True,
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": config.expected_output_tokens,
        "messages": messages,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://cascade.local",
        "X-Title": "Cascade Gate Fix",
    }

    response_metadata: dict[str, object] = {
        "status_code": None,
        "response_headers": {},
        "response_id": None,
        "response_model": None,
        "finish_reason": None,
        "chunk_count": 0,
        "stream_log": f"gate_fix_attempt_{attempt_number}.stream.log",
    }

    stream_log_path = run_dir / f"gate_fix_attempt_{attempt_number}.stream.log"
    full_response = ""
    stream_buffer = ""
    flush_threshold = 48

    if config.debug:
        print(f"[gate-fix] OpenRouter request starting for model {model}")

    try:
        response = requests.post(
            url,
            headers=headers,
            json=request_metadata,
            stream=True,
            timeout=(10, 180),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValueError(f"OpenRouter API error: {exc}") from exc

    response_metadata["status_code"] = response.status_code
    response_metadata["response_headers"] = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"x-request-id", "content-type", "x-ratelimit-remaining", "x-ratelimit-limit"}
    }

    with stream_log_path.open("w", encoding="utf-8") as handle:
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            response_metadata["chunk_count"] = int(response_metadata["chunk_count"] or 0) + 1
            raw_line = str(line)
            handle.write(raw_line + "\n")
            handle.flush()

            if raw_line.startswith("data: "):
                raw_line = raw_line[6:]
            if raw_line == "[DONE]":
                continue

            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if response_metadata["response_id"] is None and payload.get("id") is not None:
                response_metadata["response_id"] = payload.get("id")
            if response_metadata["response_model"] is None and payload.get("model") is not None:
                response_metadata["response_model"] = payload.get("model")

            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                response_metadata["finish_reason"] = finish_reason

            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if not isinstance(content, str) or not content:
                continue

            full_response += content
            if config.stream:
                stream_buffer += content
                stream_buffer = _flush_model_stream_buffer(
                    stream_buffer,
                    force=False,
                    flush_threshold=flush_threshold,
                )

            if finish_reason and config.stream:
                stream_buffer = _flush_model_stream_buffer(
                    stream_buffer,
                    force=True,
                    flush_threshold=flush_threshold,
                )

    if config.stream:
        _flush_model_stream_buffer(
            stream_buffer,
            force=True,
            flush_threshold=flush_threshold,
        )

    return full_response, request_metadata, response_metadata


def _model_profiles_by_id(
    default_profile: ModelProfile,
    explicit_models: list[str],
    model_profiles_by_id: dict[str, ModelProfile] | None,
) -> dict[str, ModelProfile]:
    profiles = {default_profile.model: default_profile}
    if model_profiles_by_id:
        profiles.update(model_profiles_by_id)
    for model in explicit_models:
        profiles.setdefault(model, default_profile)
    return profiles


def run_gate_fix_loop(
    *,
    worktree: Path,
    project_name: str,
    agent: str,
    mandate_slug: str,
    gate_command: str,
    failing_hook: str,
    failing_log: str,
    failing_category: GateFixCategory,
    config: GateFixConfig,
    model_profile: ModelProfile,
    run_dir: Path,
    gate_result: dict[str, object] | None = None,
    model_profiles_by_id: dict[str, ModelProfile] | None = None,
    failure_source: str | None = None,
) -> GateFixResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    model_chain = [config.model, *[item for item in (config.fallback_models or []) if item != config.model]]
    profiles_by_id = _model_profiles_by_id(model_profile, model_chain, model_profiles_by_id)

    print("[gate-fix] Starting gate-fix loop")
    print(f"[gate-fix] Model: {config.model}")
    print(f"[gate-fix] Max attempts: {config.max_attempts}")
    print(f"[gate-fix] Cost cap: ${config.max_estimated_cost_usd:.2f}")
    print(f"[gate-fix] Category: {failing_category.value}")

    try:
        branch = run_command("git rev-parse --abbrev-ref HEAD", cwd=worktree).stdout.strip()
    except CommandError:
        return GateFixResult(
            success=False,
            attempts=[],
            total_estimated_cost=0.0,
            stop_reason="Cannot determine current branch",
            initial_model=config.model,
            fallback_chain=model_chain[1:],
            error_message="git rev-parse failed",
        )

    original_dirty = set(get_current_dirty_files(worktree))
    baseline_diff_size = _get_diff_size(worktree)
    previous_failure_signatures: list[str] = []
    total_cost = 0.0
    attempts: list[GateFixAttempt] = []
    current_log = failing_log
    current_hook = failing_hook
    current_category = failing_category
    forced_full_file_retry_for: str | None = None
    rerun_support_files: list[str] = []
    touched_raw = [] if gate_result is None else gate_result.get("touched_files", [])
    touched_files = [str(item) for item in touched_raw] if isinstance(touched_raw, list) else []
    initial_signature = build_failure_signature(gate_result, failing_log) or ""

    mandate_payload = read_mandate_metadata(worktree, mandate_slug)
    mandate_scope_raw = [] if mandate_payload is None else mandate_payload.get("file_scope", [])
    mandate_scope = [str(item) for item in mandate_scope_raw] if isinstance(mandate_scope_raw, list) else []

    probe_passed, probe_output, probe_hook, probe_category, probe_model_fixable, probe_signature, probe_command = _run_current_gate_probe(
        worktree=worktree,
        run_dir=run_dir,
        gate_command=gate_command,
        failing_hook=current_hook,
        fallback_category=current_category,
        gate_result=gate_result,
        failure_source=failure_source,
    )
    current_failure_signature = probe_signature or initial_signature
    if probe_passed:
        print("[pass] Gate probe passed without model call")
        return GateFixResult(
            success=True,
            attempts=[],
            total_estimated_cost=0.0,
            stop_reason="Gate passed during current probe",
            initial_model=config.model,
            fallback_chain=model_chain[1:],
        )
    current_log = probe_output or current_log
    current_hook = probe_hook
    current_category = probe_category
    if not probe_model_fixable:
        return GateFixResult(
            success=False,
            attempts=[],
            total_estimated_cost=0.0,
            stop_reason="Current probe is deterministic non-model failure",
            initial_model=config.model,
            fallback_chain=model_chain[1:],
            error_message=_deterministic_repair_guidance(
                rerun_output=current_log,
                project_name=project_name,
                agent=agent,
            ),
        )

    last_probe_context_hash = _compute_failure_context_hash(
        worktree=worktree,
        failing_command=probe_command,
        failure_signature=current_failure_signature,
    )

    for attempt_number in range(1, config.max_attempts + 1):
        current_context_hash = _compute_failure_context_hash(
            worktree=worktree,
            failing_command=probe_command,
            failure_signature=current_failure_signature,
        )
        if current_context_hash != last_probe_context_hash:
            probe_passed, probe_output, probe_hook, probe_category, probe_model_fixable, probe_signature, probe_command = _run_current_gate_probe(
                worktree=worktree,
                run_dir=run_dir,
                gate_command=gate_command,
                failing_hook=current_hook,
                fallback_category=current_category,
                gate_result=gate_result,
                failure_source=failure_source,
            )
            current_failure_signature = probe_signature or current_failure_signature
            last_probe_context_hash = _compute_failure_context_hash(
                worktree=worktree,
                failing_command=probe_command,
                failure_signature=current_failure_signature,
            )
            if probe_passed:
                print("[pass] Gate probe passed without model call")
                return GateFixResult(
                    success=True,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Gate passed during current probe",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                )
            current_log = probe_output or current_log
            current_hook = probe_hook
            current_category = probe_category
            if not probe_model_fixable:
                return GateFixResult(
                    success=False,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Current probe is deterministic non-model failure",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                    error_message=_deterministic_repair_guidance(
                        rerun_output=current_log,
                        project_name=project_name,
                        agent=agent,
                    ),
                )

        if forced_full_file_retry_for:
            preferred_model = config.model
        else:
            preferred_model = model_chain[min(attempt_number - 1, len(model_chain) - 1)]
        active_model = preferred_model
        active_profile = profiles_by_id.get(active_model, model_profile)
        status_summary = _get_status_summary(worktree)
        dirty_files = get_current_dirty_files(worktree)

        stale_entries = _extract_stale_complexity_baseline_entries(current_log)
        deterministic_changed_files: list[str] = []
        if stale_entries:
            removed_ok, baseline_changed, removed_entries_message = _remove_stale_complexity_baseline_entries(
                worktree=worktree,
                entries=stale_entries,
            )
            if removed_ok:
                deterministic_changed_files.extend(baseline_changed)
                for entry in stale_entries:
                    print(f"[deterministic] removed stale complexity baseline entry {entry}")
                if "config/complexity/c901-baseline.txt" not in rerun_support_files:
                    rerun_support_files.append("config/complexity/c901-baseline.txt")
            elif removed_entries_message and "No matching" not in removed_entries_message:
                print(f"[deterministic] baseline cleanup skipped: {removed_entries_message}")

        formatting_command = _extract_safe_formatting_command(current_log)
        if formatting_command is None:
            deterministic_targets = _select_candidate_files(
                worktree=worktree,
                log_text=current_log,
                touched_files=touched_files,
                dirty_files=dirty_files,
                max_files=3,
            )
            formatting_command = _build_fallback_formatting_command(
                category=current_category,
                target_files=deterministic_targets,
            )
        if formatting_command:
            formatting_ok, formatted_files, formatting_message = _run_deterministic_formatter(
                worktree=worktree,
                command=formatting_command,
                dirty_before=set(dirty_files),
            )
            if formatting_ok:
                deterministic_changed_files.extend(formatted_files)
                print(f"[deterministic] ran formatting command: {formatting_message}")
            else:
                lowered_message = formatting_message.lower()
                if "not found" in lowered_message:
                    print(f"[deterministic] formatter unavailable, falling back to model: {formatting_message}")
                else:
                    return GateFixResult(
                        success=False,
                        attempts=attempts,
                        total_estimated_cost=total_cost,
                        stop_reason="Deterministic formatting command failed",
                        initial_model=config.model,
                        fallback_chain=model_chain[1:],
                        error_message=formatting_message,
                    )

        deterministic_changed_files = list(dict.fromkeys(deterministic_changed_files))
        if deterministic_changed_files:
            stage_result = "No staging required for non-commit gate"
            if _is_commit_gate_command(gate_command):
                stage_ok, stage_message = _stage_gate_fix_files_for_commit(
                    worktree,
                    deterministic_changed_files,
                    set(dirty_files),
                )
                stage_result = stage_message
                if not stage_ok:
                    return GateFixResult(
                        success=False,
                        attempts=attempts,
                        total_estimated_cost=total_cost,
                        stop_reason="Failed to stage deterministic fixes for commit gate",
                        initial_model=config.model,
                        fallback_chain=model_chain[1:],
                        error_message=stage_message,
                    )

            print(f"[rerun] Running: {gate_command}")
            passed, rerun_output = run_gate_recheck(worktree, gate_command, run_dir, attempt_number)
            if passed:
                print("[pass] Gate passed after deterministic fix")
                return GateFixResult(
                    success=True,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Gate passed after deterministic fix",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                )

            rerun_signature = build_failure_signature(gate_result, rerun_output) or current_failure_signature
            rerun_gate_classification = classify_gate_failure(rerun_output)
            rerun_hook = str(rerun_gate_classification.get("hook") or current_hook or "unknown")
            classification_after = classify_failure_as_model_fixable(rerun_output, rerun_hook)
            rerun_model_fixable = is_model_fixable(classification_after)

            _save_attempt_failure_context(
                run_dir=run_dir,
                attempt_number=attempt_number,
                gate_command=gate_command,
                rerun_hook=rerun_hook,
                rerun_category=classification_after,
                model_fixable=rerun_model_fixable,
                rerun_signature=rerun_signature,
                rerun_output=rerun_output,
            )

            new_support_files = _extract_rerun_support_files(
                rerun_output=rerun_output,
                worktree=worktree,
                current_target_files=deterministic_changed_files,
            )
            for support_file in new_support_files:
                if support_file not in rerun_support_files:
                    rerun_support_files.append(support_file)

            if not rerun_model_fixable:
                return GateFixResult(
                    success=False,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Deterministic non-model failure after deterministic fix",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                    error_message=_deterministic_repair_guidance(
                        rerun_output=rerun_output,
                        project_name=project_name,
                        agent=agent,
                    ),
                )

            current_failure_signature = rerun_signature
            current_log = rerun_output
            current_hook = rerun_hook
            current_category = classification_after
            last_probe_context_hash = _compute_failure_context_hash(
                worktree=worktree,
                failing_command=probe_command,
                failure_signature=current_failure_signature,
            )
            print("[rerun] continue: yes")
            continue

        candidate_files = _select_candidate_files(
            worktree=worktree,
            log_text=current_log,
            touched_files=touched_files,
            dirty_files=dirty_files,
        )
        target_files, batch_reason, dominant_file, dominant = _select_batch_files(
            mode=config.batch_mode,
            category=current_category,
            log_text=current_log,
            touched_files=touched_files,
            dirty_files=dirty_files,
            candidate_files=candidate_files,
            forced_file=forced_full_file_retry_for,
        )

        expansion_additions: list[str] = []
        if rerun_support_files:
            print(f"[batch] carrying rerun support files: {', '.join(rerun_support_files)}")
            batch_set = set(target_files)
            for _exp_f in rerun_support_files:
                if _exp_f in batch_set:
                    continue
                try:
                    _exp_full = _validate_relative_path(worktree, _exp_f)
                except ValueError:
                    continue
                if _exp_full.exists() and _exp_full.is_file():
                    expansion_additions.append(_exp_f)
            if expansion_additions:
                target_files = list(dict.fromkeys([*target_files, *expansion_additions]))
                batch_reason = "expanded-rerun-evidence"

        file_contexts = _read_candidate_file_contexts(worktree, target_files)
        patch_mode_preference = _select_patch_mode_preference(
            target_files=target_files,
            force_full_file_for=forced_full_file_retry_for,
            diff_size=_get_diff_size(worktree),
        )
        print(
            f"[batch] mode={config.batch_mode.value} reason={batch_reason} "
            f"targets={', '.join(target_files) if target_files else '(none)'}"
        )
        if expansion_additions:
            print(f"[batch] expanded due to rerun evidence: {', '.join(expansion_additions)}")
        if dominant_file and dominant:
            print(f"[batch] dominant file: {dominant_file}")
        print(f"[patch-mode] preference={patch_mode_preference}")

        prompt = build_gate_fix_prompt(
            project_name=project_name,
            agent=agent,
            worktree=worktree,
            branch=branch,
            mandate_slug=mandate_slug,
            failing_command=gate_command,
            failing_hook=current_hook,
            failing_log=current_log,
            dirty_files=dirty_files,
            changed_files=touched_files,
            mandate_scope=mandate_scope,
            status_summary=status_summary,
            file_contexts=file_contexts,
            target_files=target_files,
            batch_mode=config.batch_mode,
            dominant_file=dominant_file,
            patch_mode_preference=patch_mode_preference,
        )
        _append_prompt_artifact(run_dir, attempt_number, prompt)

        prompt_tokens = estimate_tokens(prompt)
        preferred_cost = estimate_cost(prompt_tokens, config.expected_output_tokens, active_profile)

        selected_model = active_model
        selected_profile = active_profile
        estimated_cost = preferred_cost
        if total_cost + preferred_cost > config.max_estimated_cost_usd:
            model_candidates = [preferred_model, *[m for m in model_chain if m != preferred_model]]
            selected_model = ""
            selected_profile = active_profile
            estimated_cost = preferred_cost
            for candidate in model_candidates:
                candidate_profile = profiles_by_id.get(candidate, model_profile)
                candidate_cost = estimate_cost(prompt_tokens, config.expected_output_tokens, candidate_profile)
                if total_cost + candidate_cost <= config.max_estimated_cost_usd:
                    selected_model = candidate
                    selected_profile = candidate_profile
                    estimated_cost = candidate_cost
                    break

            if not selected_model:
                return GateFixResult(
                    success=False,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Estimated cost would exceed cap",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                )

        active_model = selected_model
        active_profile = selected_profile
        print(f"[gate-fix] ===== Attempt {attempt_number} of {config.max_attempts} =====")
        print(f"[gate-fix] Estimated cost: ${estimated_cost:.4f}")

        if active_model != preferred_model:
            print(f"[gate-fix] cost-aware model selection: using {active_model} (preferred {preferred_model})")

        if check_branch_drift(worktree, branch):
            return GateFixResult(
                success=False,
                attempts=attempts,
                total_estimated_cost=total_cost,
                stop_reason="Branch drift detected before model attempt",
                initial_model=config.model,
                fallback_chain=model_chain[1:],
            )

        try:
            print(f"[gate-fix] Calling {active_model}")
            streamed_output, request_metadata, response_metadata = stream_openrouter_request(
                active_model,
                [{"role": "user", "content": prompt}],
                config,
                run_dir,
                attempt_number,
            )
        except ValueError as exc:
            failure_reason = str(exc)
            attempt = GateFixAttempt(
                attempt_number=attempt_number,
                model=active_model,
                prompt_tokens=prompt_tokens,
                expected_output_tokens=config.expected_output_tokens,
                estimated_cost=estimated_cost,
                request_metadata={"model": active_model},
                response_metadata={},
                response_summary="OpenRouter request failed",
                changed_files=[],
                success=False,
                failure_reason=failure_reason,
                patch_apply_result="No patch applied.",
            )
            attempts.append(attempt)
            _write_attempt_summary(run_dir, attempt)
            if attempt_number >= len(model_chain):
                return GateFixResult(
                    success=False,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Fallback model chain exhausted",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                    error_message=failure_reason,
                )
            continue

        total_cost += estimated_cost

        if check_branch_drift(worktree, branch):
            attempt = GateFixAttempt(
                attempt_number=attempt_number,
                model=active_model,
                prompt_tokens=prompt_tokens,
                expected_output_tokens=config.expected_output_tokens,
                estimated_cost=estimated_cost,
                request_metadata=request_metadata,
                response_metadata=response_metadata,
                response_summary="Branch drift detected after model response",
                changed_files=[],
                success=False,
                failure_reason="Branch drift",
                streamed_output=streamed_output,
                patch_apply_result="No patch applied.",
            )
            attempts.append(attempt)
            _update_model_call_artifact(run_dir, selected_model=config.model, fallback_models=model_chain[1:], attempt=attempt)
            _write_attempt_summary(run_dir, attempt)
            return GateFixResult(
                success=False,
                attempts=attempts,
                total_estimated_cost=total_cost,
                stop_reason="Branch drift detected after model response",
                initial_model=config.model,
                fallback_chain=model_chain[1:],
            )

        apply_success, changed_files, apply_message = apply_model_fixes(worktree, streamed_output)
        print(f"[apply] {apply_message}")
        if changed_files:
            print(f"[apply] Changed files: {', '.join(changed_files)}")

        current_dirty = set(get_current_dirty_files(worktree))
        if apply_success and detect_unrelated_file_growth(original_dirty, current_dirty, changed_files):
            attempt = GateFixAttempt(
                attempt_number=attempt_number,
                model=active_model,
                prompt_tokens=prompt_tokens,
                expected_output_tokens=config.expected_output_tokens,
                estimated_cost=estimated_cost,
                request_metadata=request_metadata,
                response_metadata=response_metadata,
                response_summary="Suspicious unrelated file growth detected",
                changed_files=changed_files,
                success=False,
                failure_reason="Too many unrelated files modified",
                streamed_output=streamed_output,
                patch_apply_result=apply_message,
            )
            attempts.append(attempt)
            _update_model_call_artifact(run_dir, selected_model=config.model, fallback_models=model_chain[1:], attempt=attempt)
            _write_attempt_summary(run_dir, attempt)
            return GateFixResult(
                success=False,
                attempts=attempts,
                total_estimated_cost=total_cost,
                stop_reason="Suspicious unrelated file growth",
                initial_model=config.model,
                fallback_chain=model_chain[1:],
            )

        if not apply_success:
            lowered_apply_message = apply_message.lower()
            if (
                bool(target_files)
                and (
                    ("matched" in lowered_apply_message and "times" in lowered_apply_message)
                    or ("matched 0 times" in lowered_apply_message)
                    or ("matched 0" in lowered_apply_message)
                )
            ):
                forced_full_file_retry_for = target_files[0]
                print(
                    "[patch-mode] preference=full_file because previous anchored edit failed "
                    f"{forced_full_file_retry_for}"
                )
            attempt = GateFixAttempt(
                attempt_number=attempt_number,
                model=active_model,
                prompt_tokens=prompt_tokens,
                expected_output_tokens=config.expected_output_tokens,
                estimated_cost=estimated_cost,
                request_metadata=request_metadata,
                response_metadata=response_metadata,
                response_summary="Model response could not be applied safely",
                changed_files=[],
                success=False,
                failure_reason=apply_message,
                streamed_output=streamed_output,
                patch_apply_result=apply_message,
            )
            attempts.append(attempt)
            _update_model_call_artifact(run_dir, selected_model=config.model, fallback_models=model_chain[1:], attempt=attempt)
            _write_attempt_summary(run_dir, attempt)
            if attempt_number >= len(model_chain):
                return GateFixResult(
                    success=False,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Fallback model chain exhausted",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                    error_message=apply_message,
                )
            continue

        forced_full_file_retry_for = None

        stage_result = "No staging required for non-commit gate"
        if _is_commit_gate_command(gate_command):
            stage_ok, stage_message = _stage_gate_fix_files_for_commit(
                worktree,
                changed_files,
                set(dirty_files),
            )
            stage_result = stage_message
            if stage_message.startswith("Staging model-changed files"):
                print(f"[stage] {stage_message}")
            elif stage_message.startswith("No model-changed files"):
                print(f"[stage] {stage_message}")
            elif stage_message.startswith("No model-changed files met"):
                print(f"[stage] {stage_message}")
            else:
                print(f"[stage] Failed to stage model-changed files: {stage_message}")

            if not stage_ok:
                attempt = GateFixAttempt(
                    attempt_number=attempt_number,
                    model=active_model,
                    prompt_tokens=prompt_tokens,
                    expected_output_tokens=config.expected_output_tokens,
                    estimated_cost=estimated_cost,
                    request_metadata=request_metadata,
                    response_metadata=response_metadata,
                    response_summary="Failed to stage model edits for commit gate",
                    changed_files=changed_files,
                    success=False,
                    failure_reason=stage_message,
                    streamed_output=streamed_output,
                    patch_apply_result=f"{apply_message} | stage: {stage_result}",
                )
                attempts.append(attempt)
                _update_model_call_artifact(run_dir, selected_model=config.model, fallback_models=model_chain[1:], attempt=attempt)
                _write_attempt_summary(run_dir, attempt)
                return GateFixResult(
                    success=False,
                    attempts=attempts,
                    total_estimated_cost=total_cost,
                    stop_reason="Failed to stage model-changed files for commit gate",
                    initial_model=config.model,
                    fallback_chain=model_chain[1:],
                    error_message=stage_message,
                )

        print(f"[rerun] Running: {gate_command}")
        passed, rerun_output = run_gate_recheck(worktree, gate_command, run_dir, attempt_number)
        rerun_signature = None if passed else build_failure_signature(gate_result, rerun_output)
        diff_size_after = _get_diff_size(worktree)

        if passed:
            attempt = GateFixAttempt(
                attempt_number=attempt_number,
                model=active_model,
                prompt_tokens=prompt_tokens,
                expected_output_tokens=config.expected_output_tokens,
                estimated_cost=estimated_cost,
                request_metadata=request_metadata,
                response_metadata=response_metadata,
                response_summary="Gate passed",
                changed_files=changed_files,
                success=True,
                streamed_output=streamed_output,
                patch_apply_result=f"{apply_message} | stage: {stage_result}",
                rerun_command=gate_command,
                rerun_result=rerun_output,
                diff_size_after=diff_size_after,
            )
            attempts.append(attempt)
            _update_model_call_artifact(run_dir, selected_model=config.model, fallback_models=model_chain[1:], attempt=attempt)
            _write_attempt_summary(run_dir, attempt)
            print("[pass] Gate passed")
            return GateFixResult(
                success=True,
                attempts=attempts,
                total_estimated_cost=total_cost,
                stop_reason="Gate passed",
                initial_model=config.model,
                fallback_chain=model_chain[1:],
            )

        rerun_gate_classification = classify_gate_failure(rerun_output)
        rerun_hook = str(rerun_gate_classification.get("hook") or current_hook or "unknown")
        classification_after = classify_failure_as_model_fixable(rerun_output, rerun_hook)
        rerun_model_fixable = is_model_fixable(classification_after)

        print(f"[rerun] hook/check: {rerun_hook}")
        print(f"[rerun] category: {classification_after.value}")
        print(f"[rerun] model-fixable: {'yes' if rerun_model_fixable else 'no'}")

        _save_attempt_failure_context(
            run_dir=run_dir,
            attempt_number=attempt_number,
            gate_command=gate_command,
            rerun_hook=rerun_hook,
            rerun_category=classification_after,
            model_fixable=rerun_model_fixable,
            rerun_signature=rerun_signature,
            rerun_output=rerun_output,
        )

        repeated_same_failure = rerun_signature in previous_failure_signatures
        diff_expanded_without_improving = (
            classification_after == current_category
            and diff_size_after > max(baseline_diff_size * 3, baseline_diff_size + 250, 300)
        )

        failure_reason = "Gate still failing after patch application"
        stop_reason: str | None = None
        if repeated_same_failure:
            stop_reason = "Repeated same failure signature"
            failure_reason = "Same failure signature repeated after rerun"
        elif not rerun_model_fixable:
            stop_reason = "Deterministic non-model failure after rerun"
            failure_reason = _deterministic_repair_guidance(
                rerun_output=rerun_output,
                project_name=project_name,
                agent=agent,
            )
        elif diff_expanded_without_improving:
            stop_reason = "Diff expanded substantially without improving gate result"
            failure_reason = "Large diff without gate improvement"

        attempt = GateFixAttempt(
            attempt_number=attempt_number,
            model=active_model,
            prompt_tokens=prompt_tokens,
            expected_output_tokens=config.expected_output_tokens,
            estimated_cost=estimated_cost,
            request_metadata=request_metadata,
            response_metadata=response_metadata,
            response_summary="Gate still failing",
            changed_files=changed_files,
            success=False,
            failure_signature=rerun_signature,
            failure_reason=failure_reason,
            streamed_output=streamed_output,
            patch_apply_result=f"{apply_message} | stage: {stage_result}",
            rerun_command=gate_command,
            rerun_result=rerun_output,
            diff_size_after=diff_size_after,
        )
        attempts.append(attempt)
        _update_model_call_artifact(run_dir, selected_model=config.model, fallback_models=model_chain[1:], attempt=attempt)
        _write_attempt_summary(run_dir, attempt)
        print("[fail] Gate still failing")

        if stop_reason is not None:
            if stop_reason == "Deterministic non-model failure after rerun":
                print(f"[stop] deterministic repair suggested: {failure_reason}")
            else:
                print(f"[stop] {stop_reason}")
            return GateFixResult(
                success=False,
                attempts=attempts,
                total_estimated_cost=total_cost,
                stop_reason=stop_reason,
                initial_model=config.model,
                fallback_chain=model_chain[1:],
            )

        previous_failure_signatures.append(rerun_signature or "")
        # Collect files explicitly referenced in this rerun output for expansion on later attempts.
        rerun_new_support_files = _extract_rerun_support_files(
            rerun_output=rerun_output,
            worktree=worktree,
            current_target_files=target_files,
        )
        for support_file in rerun_new_support_files:
            if support_file not in rerun_support_files:
                rerun_support_files.append(support_file)
        if rerun_new_support_files:
            print(f"[batch] expanded due to rerun evidence: {', '.join(rerun_new_support_files)}")
        current_log = rerun_output
        current_hook = rerun_hook
        current_category = classification_after
        current_failure_signature = rerun_signature or current_failure_signature
        last_probe_context_hash = _compute_failure_context_hash(
            worktree=worktree,
            failing_command=probe_command,
            failure_signature=current_failure_signature,
        )
        print("[rerun] continue: yes")

    return GateFixResult(
        success=False,
        attempts=attempts,
        total_estimated_cost=total_cost,
        stop_reason=f"Max attempts ({config.max_attempts}) exceeded",
        initial_model=config.model,
        fallback_chain=model_chain[1:],
    )


def save_gate_fix_summary(run_dir: Path, result: GateFixResult) -> Path:
    summary_file = run_dir / "gate_fix_summary.json"
    summary_file.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return summary_file