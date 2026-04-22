from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cascade.config import (
    ConfigError,
    ValidationResult,
    is_inside_workspace,
    load_project_config,
    resolve_workspace_root,
    validate_project_paths,
    workspace_root_is_broad,
)


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    details: str


def run_doctor_checks(project_file: Path) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    python_ok = sys.version_info >= (3, 11)
    checks.append(
        DoctorCheck(
            name="python",
            status="ok" if python_ok else "fail",
            details=f"Detected Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    gh_path = shutil.which("gh")

    opencode_path = shutil.which("opencode")
    checks.append(
        DoctorCheck(
            name="OpenCode CLI",
            status="ok" if opencode_path else "warn",
            details=(
                opencode_path
                if opencode_path
                else "OpenCode missing: model-backed commands unavailable"
            ),
        )
    )

    project = None
    try:
        project = load_project_config(project_file)
    except ConfigError as exc:
        checks.append(DoctorCheck(name="project config", status="fail", details=str(exc)))
        return checks

    checks.append(DoctorCheck(name="project config", status="ok", details=f"Loaded project '{project.name}'"))

    gh_required = bool(project.github.owner and project.github.repo)
    checks.append(
        DoctorCheck(
            name="gh CLI",
            status="ok" if gh_path else ("fail" if gh_required else "warn"),
            details=(
                gh_path
                if gh_path
                else (
                    "GitHub CLI `gh` not found on PATH"
                    if gh_required
                    else "GitHub CLI missing but project does not require issue fetching"
                )
            ),
        )
    )

    # ── Workspace path checks ────────────────────────────────────────────────
    workspace = resolve_workspace_root(project)
    if workspace is not None:
        checks.append(
            DoctorCheck(
                name="workspace_root",
                status="ok" if workspace.exists() else "fail",
                details=str(workspace),
            )
        )
        if workspace_root_is_broad(workspace):
            checks.append(
                DoctorCheck(
                    name="workspace_root_broad",
                    status="warn",
                    details=(
                        f"workspace_root appears broad ({workspace.name!r}); "
                        "prefer a dedicated workspace such as 'instica-workspace'."
                    ),
                )
            )

    path_results: list[ValidationResult] = validate_project_paths(project)
    # Skip workspace_root itself (already added above) and broad warning
    skip_keys = {"workspace_root", "workspace_root_broad"}
    for result in path_results:
        if result.key in skip_keys:
            continue
        checks.append(DoctorCheck(name=result.key, status=result.status, details=result.message))

    if gh_path is None:
        checks.append(
            DoctorCheck(
                name="gh auth",
                status="fail" if gh_required else "warn",
                details="Skipped because `gh` is not available on PATH",
            )
        )
        return checks

    auth_result = subprocess.run(
        ["gh", "auth", "status"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    auth_output = auth_result.stdout.strip() or "No output"
    checks.append(
        DoctorCheck(
            name="gh auth",
            status="ok" if auth_result.returncode == 0 else "fail",
            details=auth_output.splitlines()[0],
        )
    )
    return checks


def has_failures(checks: list[DoctorCheck]) -> bool:
    return any(check.status == "fail" for check in checks)
