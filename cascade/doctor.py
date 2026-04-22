from __future__ import annotations

import os
import re
import shlex
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


def _running_in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _docker_socket_path() -> Path:
    return Path("/var/run/docker.sock")


def _repo_uses_ssh_remote(repo_root: Path) -> bool:
    remote_result = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    remote_url = remote_result.stdout.strip()
    return remote_result.returncode == 0 and (
        remote_url.startswith("git@") or remote_url.startswith("ssh://")
    )


def _origin_default_branch(repo_root: Path) -> str | None:
    symbolic_result = subprocess.run(
        ["git", "-C", str(repo_root), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if symbolic_result.returncode != 0:
        return None
    value = symbolic_result.stdout.strip()
    if not value.startswith("origin/"):
        return None
    return value.split("/", maxsplit=1)[1]


def _extract_make_target(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "make":
        return None

    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        if "=" in token:
            continue
        return token
    return None


def _makefile_has_target(makefile_path: Path, target: str) -> bool:
    if not makefile_path.exists():
        return False
    pattern = re.compile(rf"^\s*{re.escape(target)}\s*:")
    try:
        for line in makefile_path.read_text(encoding="utf-8").splitlines():
            if pattern.match(line):
                return True
    except OSError:
        return False
    return False


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

    gh_token_present = bool(os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"))
    checks.append(
        DoctorCheck(
            name="GitHub token env",
            status="ok" if gh_token_present else "warn",
            details=(
                "GH_TOKEN/GITHUB_TOKEN present"
                if gh_token_present
                else "GH_TOKEN and GITHUB_TOKEN are missing from environment"
            ),
        )
    )

    model_token_present = bool(
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    checks.append(
        DoctorCheck(
            name="model token env",
            status="ok" if model_token_present else "warn",
            details=(
                "At least one model API token is present"
                if model_token_present
                else "No model API token found in environment"
            ),
        )
    )

    if _running_in_docker():
        docker_path = shutil.which("docker")
        checks.append(
            DoctorCheck(
                name="docker CLI",
                status="ok" if docker_path else "fail",
                details=(
                    docker_path
                    if docker_path
                    else "Docker CLI missing inside Cascade container; rebuild the image with host Docker support."
                ),
            )
        )

        docker_socket_path = _docker_socket_path()
        docker_socket_ok = docker_socket_path.exists() and os.access(docker_socket_path, os.R_OK | os.W_OK)
        checks.append(
            DoctorCheck(
                name="docker socket",
                status="ok" if docker_socket_ok else "fail",
                details=(
                    f"{docker_socket_path} is mounted and readable"
                    if docker_socket_ok
                    else (
                        "Missing or unreadable /var/run/docker.sock in Docker container. "
                        "Mount the host Docker socket so target repo Docker commands can use the host daemon."
                    )
                ),
            )
        )

        if docker_path:
            docker_info_result = subprocess.run(
                ["docker", "info"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            docker_info_output = (docker_info_result.stdout or "").strip()
            checks.append(
                DoctorCheck(
                    name="docker info",
                    status="ok" if docker_info_result.returncode == 0 else "fail",
                    details=(
                        "Connected to host Docker daemon"
                        if docker_info_result.returncode == 0
                        else (
                            docker_info_output.splitlines()[0]
                            if docker_info_output
                            else "docker info failed"
                        )
                    ),
                )
            )

        ssh_config_path = Path("/root/.ssh/config")
        ssh_config_exists = ssh_config_path.exists()
        checks.append(
            DoctorCheck(
                name="docker ssh config",
                status="ok" if ssh_config_exists else "warn",
                details=(
                    str(ssh_config_path)
                    if ssh_config_exists
                    else (
                        "Missing /root/.ssh/config in Docker container. "
                        "Run make ssh-config on the host so Docker can mount ~/.cascade/ssh/config."
                    )
                ),
            )
        )

        if ssh_config_exists:
            parse_result = subprocess.run(
                ["ssh", "-G", "github.com"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            parse_output = (parse_result.stdout or "").strip()
            output_lower = parse_output.lower()
            if "bad configuration option" in output_lower and "usekeychain" in output_lower:
                checks.append(
                    DoctorCheck(
                        name="docker ssh parse",
                        status="warn",
                        details=(
                            "Docker SSH config contains macOS-only UseKeychain. "
                            "Run make ssh-config on the host so Docker uses ~/.cascade/ssh/config."
                        ),
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="docker ssh parse",
                        status="ok" if parse_result.returncode == 0 else "warn",
                        details=(
                            "OpenSSH parsed /root/.ssh/config"
                            if parse_result.returncode == 0
                            else (parse_output.splitlines()[0] if parse_output else "ssh -G github.com failed")
                        ),
                    )
                )

            auth_result = subprocess.run(
                ["ssh", "-T", "git@github.com"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            auth_output = (auth_result.stdout or "").strip()
            auth_lower = auth_output.lower()
            auth_ok = (
                auth_result.returncode == 0
                or "successfully authenticated" in auth_lower
                or "shell access is not supported" in auth_lower
            )
            checks.append(
                DoctorCheck(
                    name="docker ssh github auth",
                    status="ok" if auth_ok else "warn",
                    details=(
                        "GitHub SSH auth reachable"
                        if auth_ok
                        else (auth_output.splitlines()[0] if auth_output else "ssh -T git@github.com failed")
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

    mandate_command_template = (
        project.commands.mandate_start
        or project.commands.start_mandate
        or project.commands.init_mandate
    )
    if mandate_command_template is not None:
        makefile_path = project.paths.repo_root / "Makefile"
        target = _extract_make_target(mandate_command_template)
        if target is None:
            checks.append(
                DoctorCheck(
                    name="mandate_start target",
                    status="warn",
                    details=(
                        "A mandate-start command is configured, but no Make target could be parsed. "
                        "Verify the command manually."
                    ),
                )
            )
        elif _makefile_has_target(makefile_path, target):
            checks.append(
                DoctorCheck(
                    name="mandate_start target",
                    status="ok",
                    details=f"Found Make target '{target}' in {makefile_path}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="mandate_start target",
                    status="warn",
                    details=(
                        f"Configured mandate-start command references make target '{target}', "
                        f"but it was not found in {makefile_path}."
                    ),
                )
            )

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

    if _running_in_docker() and project.paths.repo_root.exists() and _repo_uses_ssh_remote(project.paths.repo_root):
        default_branch = _origin_default_branch(project.paths.repo_root)
        if default_branch is None:
            checks.append(
                DoctorCheck(
                    name="docker repo fetch",
                    status="warn",
                    details="Unable to resolve origin default branch for fetch validation",
                )
            )
        else:
            fetch_result = subprocess.run(
                ["git", "-C", str(project.paths.repo_root), "fetch", "origin", default_branch],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            fetch_output = (fetch_result.stdout or "").strip()
            checks.append(
                DoctorCheck(
                    name="docker repo fetch",
                    status="ok" if fetch_result.returncode == 0 else "warn",
                    details=(
                        f"Fetched origin/{default_branch}"
                        if fetch_result.returncode == 0
                        else (
                            fetch_output.splitlines()[0]
                            if fetch_output
                            else f"git fetch origin {default_branch} failed"
                        )
                    ),
                )
            )

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
