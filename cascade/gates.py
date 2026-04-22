"""Deterministic gate result storage, staleness tracking, and failure summary.

Gate pass/fail is determined solely by command exit code.
Model output is never treated as a pass/fail signal.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from cascade.shell import CommandError, run_command


GATE_RESULT_FILENAME = "gate_result.json"


# ---------------------------------------------------------------------------
# Git helpers (deterministic, no model calls)
# ---------------------------------------------------------------------------


def get_git_head_sha(worktree: Path) -> str:
    """Return the full SHA of HEAD in the worktree, or '(unknown)' on error."""
    try:
        return run_command("git rev-parse HEAD", cwd=worktree).stdout.strip()
    except CommandError:
        return "(unknown)"


def get_diff_fingerprint(worktree: Path) -> str:
    """SHA-256 fingerprint of 'git diff HEAD' output.

    Captures both staged and unstaged changes relative to the last commit.
    Changes if any tracked file is modified, added, or deleted.
    """
    try:
        diff = run_command("git diff HEAD", cwd=worktree).stdout
    except CommandError:
        return "(unknown)"
    return hashlib.sha256(diff.encode()).hexdigest()


def get_touched_files(worktree: Path) -> list[str]:
    """Files changed relative to HEAD in the worktree (tracked files only)."""
    try:
        output = run_command("git diff --name-only HEAD", cwd=worktree).stdout.strip()
    except CommandError:
        return []
    return [line for line in output.splitlines() if line]


# ---------------------------------------------------------------------------
# Gate result storage
# ---------------------------------------------------------------------------


def save_gate_result(run_dir: Path, gate_data: dict[str, object]) -> Path:
    """Persist gate result metadata to gate_result.json in the run directory."""
    result_path = run_dir / GATE_RESULT_FILENAME
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(gate_data, indent=2) + "\n", encoding="utf-8")
    return result_path


def load_gate_result(run_dir: Path) -> dict[str, object] | None:
    """Load gate result from run directory. Returns None if not found or invalid."""
    result_path = run_dir / GATE_RESULT_FILENAME
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


# ---------------------------------------------------------------------------
# Staleness tracking
# ---------------------------------------------------------------------------


def check_gate_staleness(
    gate_result: dict[str, object], worktree: Path
) -> tuple[bool, str]:
    """Return (is_stale, reason).

    A passing gate is stale if HEAD or the working-tree diff has changed since
    the gate ran.  A failed gate is never reported as stale — the failure is
    always current.
    """
    if not gate_result.get("passed"):
        return False, ""

    saved_head = str(gate_result.get("git_head_sha", ""))
    current_head = get_git_head_sha(worktree)
    if saved_head not in ("(unknown)", "") and saved_head != current_head:
        return (
            True,
            f"HEAD changed since gate passed (was {saved_head[:8]}, now {current_head[:8]}).",
        )

    saved_fp = str(gate_result.get("diff_fingerprint", ""))
    current_fp = get_diff_fingerprint(worktree)
    if saved_fp not in ("(unknown)", "") and saved_fp != current_fp:
        return True, "Working-tree diff changed since gate passed."

    return False, ""


# ---------------------------------------------------------------------------
# Failure summary (deterministic, no model calls)
# ---------------------------------------------------------------------------


def build_failure_summary(gate_result: dict[str, object], log_content: str) -> str:
    """Produce a human-readable gate failure summary from stored metadata and log text.

    This is fully deterministic — no model is involved.
    """
    lines: list[str] = []

    cmd = gate_result.get("command", "(unknown)")
    exit_code = gate_result.get("exit_code", "(unknown)")
    log_path = gate_result.get("log_path", "(unknown)")
    touched_raw = gate_result.get("touched_files", [])
    touched: list[str] = list(touched_raw) if isinstance(touched_raw, list) else []

    lines.append(f"Gate command : {cmd}")
    lines.append(f"Exit code    : {exit_code}")
    lines.append(f"Log file     : {log_path}")

    if touched:
        lines.append("Touched files:")
        for f in touched:
            lines.append(f"  {f}")

    failed_hooks = _extract_failed_hooks(log_content)
    if failed_hooks:
        lines.append("Failing hooks/checks detected:")
        for hook in failed_hooks:
            lines.append(f"  - {hook}")

    return "\n".join(lines)


def _extract_failed_hooks(log_content: str) -> list[str]:
    """Extract hook or check names from pre-commit-style failure output.

    Matches lines like:
      Failed: ruff-format
      FAILED: mypy
      - hook id: bandit (exit code 1)
    """
    hooks: list[str] = []
    for line in log_content.splitlines():
        stripped = line.strip()
        m = re.match(r"^(?:Failed|FAILED)[:\s]+(.+)", stripped, re.IGNORECASE)
        if m:
            hooks.append(m.group(1).strip())
            continue
        m2 = re.match(r"^-\s+hook id:\s+(.+?)(?:\s+\(|$)", stripped)
        if m2:
            name = m2.group(1).strip()
            if name not in hooks:
                hooks.append(name)
    return hooks


# ---------------------------------------------------------------------------
# Gate failure classification (deterministic, no model calls)
# ---------------------------------------------------------------------------

# Mapping of hook-name patterns to (category, model_recommended, suggested_action)
_HOOK_CLASSIFICATIONS: list[tuple[str, str, bool, str]] = [
    # (pattern_substring, category, model_recommended, suggested_no_model_action)
    ("trailing-whitespace", "formatting", False, "Run `pre-commit run trailing-whitespace` or strip whitespace."),
    ("end-of-file-fixer", "formatting", False, "Run `pre-commit run end-of-file-fixer` to add missing newlines."),
    ("mixed-line-ending", "formatting", False, "Run `pre-commit run mixed-line-ending` to normalize line endings."),
    ("check-yaml", "syntax", False, "Validate YAML syntax in the failing file."),
    ("check-json", "syntax", False, "Validate JSON syntax in the failing file."),
    ("check-toml", "syntax", False, "Validate TOML syntax in the failing file."),
    ("check-merge-conflict", "policy", False, "Resolve merge conflict markers manually."),
    ("check-added-large-files", "policy", False, "Remove or LFS-track the large file."),
    ("debug-statements", "policy", False, "Remove debug statements (print/pdb/breakpoint) from staged files."),
    ("no-commit-to-branch", "policy", False, "Switch to the correct feature branch before committing."),
    ("mandate-commit-msg", "policy", False, "Fix the commit message to match the required format."),
    ("ruff-format", "formatting", False, "Run `ruff format <files>` to auto-format."),
    ("ruff", "linting", False, "Run `ruff check --fix <files>` to auto-fix where possible."),
    ("pyright", "typing", True, "Review and fix type errors flagged by pyright in the changed files."),
    ("mypy", "typing", True, "Review and fix type errors flagged by mypy."),
    ("bandit", "security", True, "Review bandit findings; do not auto-fix blindly — security-sensitive."),
    ("gitleaks", "security", True, "Review for leaked secrets; do not auto-fix blindly — security-sensitive."),
    ("detect-private-key", "security", True, "Remove any private keys; review carefully — security-sensitive."),
    ("jungle-migrate-check", "migration", True, "Create or fix the required Django migration."),
    ("migrate", "migration", True, "Create or fix the required Django migration."),
    ("jungle-frontend-lint", "linting", True, "Fix ESLint errors in changed frontend files."),
    ("frontend-lint", "linting", True, "Fix ESLint errors in changed frontend files."),
    ("jungle-frontend-type-coverage", "coverage", True, "Add or fix TypeScript types in changed frontend files."),
    ("frontend-type-coverage", "coverage", True, "Add or fix TypeScript types in changed frontend files."),
    ("jungle-backend-docstring", "coverage", True, "Add or fix docstrings in changed backend files."),
    ("backend-docstring", "coverage", True, "Add or fix docstrings in changed backend files."),
    ("frontend-coverage-policy", "coverage", True, "Increase frontend test coverage for changed files."),
    ("coverage", "coverage", True, "Increase test coverage for changed files."),
    ("shellcheck", "linting", False, "Fix shellcheck warnings in shell scripts."),
    ("hadolint", "linting", False, "Fix hadolint warnings in Dockerfiles."),
    ("actionlint", "linting", False, "Fix GitHub Actions YAML linting errors."),
    ("staged-type-suppression", "policy", True, "Remove broad type suppressions from staged code."),
    ("backend-docstring", "coverage", True, "Add missing docstrings to new backend functions."),
]


def classify_gate_failure(log_text: str) -> dict[str, object]:
    """Classify a gate/pre-commit failure from log text.

    Returns a dict with:
        detected: bool
        hook: str or None
        category: str
        suggested_no_model_action: str
        model_recommended: bool

    Detection is case-insensitive and matches the first recognizable hook name.
    """
    if not log_text.strip():
        return {
            "detected": False,
            "hook": None,
            "category": "unknown",
            "suggested_no_model_action": "Inspect the gate log manually.",
            "model_recommended": False,
        }

    workflow_markers = [
        "required mandate metadata is missing",
        "canonical mandate file is missing",
    ]
    log_lower = log_text.lower()
    if any(marker in log_lower for marker in workflow_markers):
        return {
            "detected": True,
            "hook": "mandate-metadata",
            "category": "workflow",
            "suggested_no_model_action": "Run `cascade repair <agent> --project <project>`.",
            "model_recommended": False,
        }

    failed_hooks = _extract_failed_hooks(log_text)
    # Also search the full log text for hook name substrings

    # Try to match against known patterns
    for hook_name in failed_hooks:
        hook_lower = hook_name.lower()
        for pattern, category, model_recommended, action in _HOOK_CLASSIFICATIONS:
            if pattern in hook_lower:
                return {
                    "detected": True,
                    "hook": hook_name,
                    "category": category,
                    "suggested_no_model_action": action,
                    "model_recommended": model_recommended,
                }

    # Fall back to scanning log text for known substrings
    for pattern, category, model_recommended, action in _HOOK_CLASSIFICATIONS:
        if pattern in log_lower:
            return {
                "detected": True,
                "hook": pattern,
                "category": category,
                "suggested_no_model_action": action,
                "model_recommended": model_recommended,
            }

    hook_guess = failed_hooks[0] if failed_hooks else None
    return {
        "detected": bool(hook_guess),
        "hook": hook_guess,
        "category": "unknown",
        "suggested_no_model_action": "Inspect the gate log manually and fix the failing check.",
        "model_recommended": True,
    }


# ---------------------------------------------------------------------------
# Status line helper (for table display)
# ---------------------------------------------------------------------------


def gate_status_line(gate_result: dict[str, object] | None, worktree: Path | None) -> str:
    """Return a compact gate status string for display in the status table."""
    if gate_result is None:
        return "no result"

    passed = bool(gate_result.get("passed"))
    timestamp = str(gate_result.get("timestamp", ""))
    short_ts = timestamp[:16] if timestamp else "?"

    if not passed:
        exit_code = gate_result.get("exit_code", "?")
        return f"FAILED (exit {exit_code}) at {short_ts}"

    if worktree is not None and worktree.exists():
        is_stale, reason = check_gate_staleness(gate_result, worktree)
        if is_stale:
            return f"STALE at {short_ts} — {reason}"

    return f"passed at {short_ts}"
