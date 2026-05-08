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


def is_file_tracked(worktree: Path, file_path: str) -> bool:
    """Check if a file is tracked by git (not untracked).
    
    Returns True if file is tracked (modified, added, deleted, staged).
    Returns False if file is untracked or doesn't exist.
    """
    try:
        # Check if file is in git index or HEAD
        run_command(f"git ls-files --error-unmatch {file_path}", cwd=worktree)
        return True
    except CommandError:
        return False


def is_file_dirty(worktree: Path, file_path: str) -> bool:
    """Check if a file has uncommitted changes (modified, deleted, untracked).
    
    Returns True if file has any changes relative to HEAD.
    Returns False if file is clean.
    """
    try:
        # Check git status for this specific file
        status_output = run_command("git status --porcelain", cwd=worktree).stdout
        for line in status_output.splitlines():
            if line.strip():
                # Status lines are " XY PATH"
                status_file = line[3:].strip() if len(line) > 3 else ""
                if status_file == file_path:
                    return True
        return False
    except CommandError:
        return False


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
    ("shellcheck", "linting", False, "Fix shellcheck warnings in shell scripts."),
    ("hadolint", "linting", False, "Fix hadolint warnings in Dockerfiles."),
    ("actionlint", "linting", False, "Fix GitHub Actions YAML linting errors."),
    ("staged-type-suppression", "policy", True, "Remove broad type suppressions from staged code."),
    ("backend-docstring", "coverage", True, "Add missing docstrings to new backend functions."),
]


def _has_explicit_coverage_failure(log_text: str) -> bool:
    coverage_failure_patterns = [
        r"(?im)^.*(?:fail|failed|error).*(?:coverage|covered).*$",
        r"(?im)^.*(?:coverage|covered).*(?:below|under|threshold|required|minimum).*$",
        r"(?im)^.*coverage\s+policy\s+failed.*$",
        r"(?im)^.*insufficient\s+coverage.*$",
    ]
    return any(re.search(pattern, log_text) is not None for pattern in coverage_failure_patterns)


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

    docker_buildkit_patterns = [
        r"(?im)the --mount option requires buildkit",
        r"(?im)docker compose requires buildx plugin",
    ]
    docker_runtime_network_patterns = [
        r"(?im)error response from daemon:.*network.*active endpoints",
        r"(?im)error response from daemon:.*is not connected to the network",
        r"(?im)docker compose.*(network|container).*(failed|error)",
        r"(?im)relational_db did not become ready",
    ]
    metadata_dirty_patterns = [
        r"(?im)^\?\?\s+\.github/mandates/[^\s]+\.json\s*$",
        r"(?im)^[ MARCUD\?]{1,2}\s+\.github/mandates/[^\s]+\s*$",
        r"(?im)mandate\s+metadata",
    ]
    workspace_link_patterns = [
        r"(?im)env file\s+.*jungle-worktrees/jungle-secrets/.*not found",
        r"(?im)no such file or directory.*jungle-worktrees/jungle-secrets",
        r"(?im)no such file or directory.*jungle-secrets.*\.env\.local",
    ]
    workflow_markers = [
        "required mandate metadata is missing",
        "canonical mandate file is missing",
    ]
    metadata_validation_patterns = [
        r"(?im)mandate\s+[^\s]+\s+is\s+not\s+in\s+progress",
        r"(?im)mandate\s+metadata\s+validation:",
        r"(?im)repo\s+mismatch:\s+expected\s+'[^']+'\s*,\s*found\s+'[^']+'",
    ]
    validation_slot_timeout_patterns = [
        r"(?im)timed out waiting for the shared heavy validation slot",
        r"(?im)shared heavy validation slot",
    ]
    branch_mismatch_patterns = [
        r"(?im)branch mismatch: expected 'agent/",
        r"(?im)expected 'agent/.+'[, ]+found 'agent/",
        r"(?im)mandate-agent-branch-mismatch",
        r"(?im)does not match mandate agent branch",
    ]
    stale_docker_state_patterns = [
        r"(?im)/workspace/jungle",
        r"(?im)no such file or directory.*?/workspace/",
    ]
    stale_worktree_gitdir_patterns = [
        r"(?im)not a git repository",
        r"(?im)fatal: not a git repository",
    ]
    log_lower = log_text.lower()

    if any(re.search(pattern, log_text) is not None for pattern in docker_buildkit_patterns):
        return {
            "detected": True,
            "hook": "docker-buildkit",
            "category": "environment",
            "strategy": "deterministic_repair",
            "repair_kind": "docker-buildkit",
            "suggested_no_model_action": (
                "Docker BuildKit is not enabled or the buildx plugin is missing. "
                "Set DOCKER_BUILDKIT=1 and COMPOSE_DOCKER_CLI_BUILD=1 in .env, and rebuild the "
                "Cascade image so docker-buildx-plugin is installed."
            ),
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in docker_runtime_network_patterns):
        return {
            "detected": True,
            "hook": "docker-runtime-network",
            "category": "environment",
            "strategy": "deterministic_retry",
            "repair_kind": "docker-runtime-network",
            "suggested_no_model_action": (
                "Docker runtime network/container startup failed during preflight. "
                "Restart or clean Docker Compose services for the mandate worktree and rerun preflight."
            ),
            "model_recommended": False,
        }

    dirty_closeout_match = re.search(
        r"Unexpected dirty file while closing mandate:\s*(?P<path>.+)",
        log_text,
        re.IGNORECASE,
    )
    if dirty_closeout_match is not None:
        dirty_file_path = dirty_closeout_match.group("path").strip()
        return {
            "detected": True,
            "hook": "mandate-dirty-file",
            "category": "workflow",
            "strategy": "deterministic_repair",
            "repair_kind": "closeout-dirty-file",
            "dirty_file_path": dirty_file_path,
            "suggested_no_model_action": (
                "Run cascade closeout-prep <agent> --project <project> --stage to classify and stage mandate-owned "
                "files, review any suspicious extras, then run cascade closeout-prep <agent> --project <project> "
                "--stage --commit --yes when safe, and rerun preflight."
            ),
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in metadata_dirty_patterns):
        return {
            "detected": True,
            "hook": "mandate-metadata",
            "category": "workflow",
            "strategy": "deterministic_repair",
            "repair_kind": "missing-mandate-metadata",
            "suggested_no_model_action": (
                "Inspect git status, confirm mandate metadata file intent, then run the configured "
                "closeout or metadata command if available; otherwise stop for human review."
            ),
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in workspace_link_patterns):
        return {
            "detected": True,
            "hook": "missing-workspace-link",
            "category": "environment",
            "strategy": "deterministic_repair",
            "repair_kind": "missing-workspace-link",
            "suggested_no_model_action": "Run `cascade repair <agent> --project <project> --kind missing-workspace-link`.",
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in validation_slot_timeout_patterns):
        return {
            "detected": True,
            "hook": "validation-slot-timeout",
            "category": "environment",
            "strategy": "deterministic_retry",
            "repair_kind": "validation-slot-timeout",
            "suggested_no_model_action": (
                "Validation lock timeout detected. Retry preflight with backoff and inspect lock owner/path from logs."
            ),
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in branch_mismatch_patterns):
        return {
            "detected": True,
            "hook": "mandate-agent-branch-mismatch",
            "category": "workflow",
            "strategy": "stop_requires_human",
            "repair_kind": "mandate-agent-branch-mismatch",
            "suggested_no_model_action": "Switch back to the exact assigned agent branch and rerun preflight.",
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in stale_docker_state_patterns):
        return {
            "detected": True,
            "hook": "stale-docker-era-state",
            "category": "environment",
            "strategy": "deterministic_repair",
            "repair_kind": "docker-era-state",
            "suggested_no_model_action": "Run docker-era-state migration repair and retry.",
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in stale_worktree_gitdir_patterns):
        return {
            "detected": True,
            "hook": "stale-worktree-gitdir",
            "category": "environment",
            "strategy": "stop_requires_human",
            "repair_kind": "stale-worktree-gitdir",
            "suggested_no_model_action": "Repair or recreate the worktree gitdir before rerunning preflight.",
            "model_recommended": False,
        }

    if any(re.search(pattern, log_text) is not None for pattern in metadata_validation_patterns):
        return {
            "detected": True,
            "hook": "mandate-metadata",
            "category": "workflow",
            "strategy": "deterministic_repair",
            "repair_kind": "mandate-metadata",
            "suggested_no_model_action": (
                "Repair mandate metadata fields (status/repo/branch/active_branch/worktree_path) and rerun preflight."
            ),
            "model_recommended": False,
        }

    if any(marker in log_lower for marker in workflow_markers):
        return {
            "detected": True,
            "hook": "mandate-metadata",
            "category": "workflow",
            "strategy": "deterministic_repair",
            "repair_kind": "missing-mandate-metadata",
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

    if _has_explicit_coverage_failure(log_text):
        return {
            "detected": True,
            "hook": "coverage-policy",
            "category": "coverage",
            "suggested_no_model_action": "Increase test coverage for changed files and rerun the configured coverage checks.",
            "model_recommended": True,
        }

    hook_guess = failed_hooks[0] if failed_hooks else None
    return {
        "detected": bool(hook_guess),
        "hook": hook_guess,
        "category": "unknown",
        "suggested_no_model_action": "Inspect the gate log manually and fix the failing check.",
        "model_recommended": True,
    }


def _first_meaningful_error_line(log_tail: str) -> str:
    for line in log_tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith(("# preflight run", "timestamp:", "command:", "exit_code:")):
            continue
        if lower.startswith(("failed:", "failed ", "- hook id:")):
            continue
        return stripped
    return ""


def failure_signature(gate_result: dict[str, object] | None, log_tail: str) -> str:
    """Build a stable signature for repeated-failure detection.

    Signature uses classification hook/category, touched files, and the first
    meaningful error line. Falls back to a hash of the log tail when needed.
    """
    classification = classify_gate_failure(log_tail)
    hook = str(classification.get("hook") or "unknown")
    category = str(classification.get("category") or "unknown")

    touched_raw = [] if gate_result is None else gate_result.get("touched_files", [])
    touched = sorted(str(path) for path in touched_raw) if isinstance(touched_raw, list) else []

    first_line = _first_meaningful_error_line(log_tail)
    if not first_line:
        first_line = hashlib.sha256(log_tail.encode()).hexdigest()[:16]

    touched_key = ",".join(touched[:10])
    return f"{category}|{hook}|{touched_key}|{first_line}"


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
