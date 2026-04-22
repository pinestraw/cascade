from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig
from cascade.doctor import has_failures, run_doctor_checks
from cascade.standards import validate_worktree_location


def _write_project_file(tmp_path: Path) -> Path:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def test_doctor_missing_opencode_is_warning_not_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _write_project_file(tmp_path)

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return None
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: False)

    checks = run_doctor_checks(project_file)
    opencode_check = [check for check in checks if check.name == "OpenCode CLI"][0]

    assert opencode_check.status == "warn"
    assert not has_failures(checks)


def test_validate_worktree_location_outside_root_rejected(tmp_path: Path) -> None:
    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "worktrees"),
        commands=CommandsConfig(create_worktree="echo create"),
    )
    outside = tmp_path / "other" / "agent-worktree"

    is_valid, message = validate_worktree_location(project, outside)

    assert not is_valid
    assert "outside configured worktree_root" in message


def test_doctor_warns_usekeychain_in_docker_ssh_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _write_project_file(tmp_path)

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return "/usr/local/bin/opencode"
        if name == "docker":
            return "/usr/bin/docker"
        return None

    class _Completed:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _run(args, **kwargs):
        joined = " ".join(args)
        if args[:2] == ["docker", "info"]:
            return _Completed(0, "Server Version: 27.0.0")
        if args[:2] == ["ssh", "-G"]:
            return _Completed(255, "Bad configuration option: UseKeychain")
        if args[:2] == ["ssh", "-T"]:
            return _Completed(1, "Hi user! You've successfully authenticated, but GitHub does not provide shell access.")
        if args[:3] == ["gh", "auth", "status"]:
            return _Completed(0, "authenticated")
        if "remote.origin.url" in joined:
            return _Completed(0, "git@github.com:pinestraw/jungle.git")
        if "symbolic-ref" in joined:
            return _Completed(0, "origin/main")
        if "fetch origin" in joined:
            return _Completed(0, "")
        return _Completed(0, "ok")

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", _run)
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda path, mode: True)

    checks = run_doctor_checks(project_file)
    docker_cli_check = next(check for check in checks if check.name == "docker CLI")
    docker_socket_check = next(check for check in checks if check.name == "docker socket")
    docker_info_check = next(check for check in checks if check.name == "docker info")
    parse_check = next(check for check in checks if check.name == "docker ssh parse")

    assert docker_cli_check.status == "ok"
    assert docker_socket_check.status == "ok"
    assert docker_info_check.status == "ok"
    assert parse_check.status == "warn"
    assert "UseKeychain" in parse_check.details


def test_doctor_reports_docker_ssh_checks_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _write_project_file(tmp_path)

    def _which(name: str):
        if name in {"gh", "opencode", "docker"}:
            return f"/usr/bin/{name}"
        return None

    class _Completed:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _run(args, **kwargs):
        joined = " ".join(args)
        if args[:2] == ["docker", "info"]:
            return _Completed(0, "Server Version: 27.0.0")
        if args[:2] == ["ssh", "-G"]:
            return _Completed(0, "user git")
        if args[:2] == ["ssh", "-T"]:
            return _Completed(1, "You've successfully authenticated, but GitHub does not provide shell access.")
        if args[:3] == ["gh", "auth", "status"]:
            return _Completed(0, "authenticated")
        if "remote.origin.url" in joined:
            return _Completed(0, "git@github.com:pinestraw/jungle.git")
        if "symbolic-ref" in joined:
            return _Completed(0, "origin/main")
        if "fetch origin" in joined:
            return _Completed(0, "")
        return _Completed(0, "ok")

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", _run)
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda path, mode: True)

    checks = run_doctor_checks(project_file)
    docker_cli_check = next(check for check in checks if check.name == "docker CLI")
    docker_socket_check = next(check for check in checks if check.name == "docker socket")
    docker_info_check = next(check for check in checks if check.name == "docker info")
    parse_check = next(check for check in checks if check.name == "docker ssh parse")
    auth_check = next(check for check in checks if check.name == "docker ssh github auth")
    fetch_check = next(check for check in checks if check.name == "docker repo fetch")

    assert docker_cli_check.status == "ok"
    assert docker_socket_check.status == "ok"
    assert docker_info_check.status == "ok"
    assert parse_check.status == "ok"
    assert auth_check.status == "ok"
    assert fetch_check.status == "ok"


def test_doctor_reports_missing_host_docker_access_in_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _write_project_file(tmp_path)

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return "/usr/local/bin/opencode"
        if name == "docker":
            return None
        return None

    class _Completed:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _run(args, **kwargs):
        joined = " ".join(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _Completed(0, "authenticated")
        if "remote.origin.url" in joined:
            return _Completed(0, "https://github.com/pinestraw/jungle.git")
        return _Completed(0, "ok")

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", _run)
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr(
        "cascade.doctor.Path.exists",
        lambda self: False if str(self) == "/var/run/docker.sock" else True,
    )
    monkeypatch.setattr("cascade.doctor.os.access", lambda path, mode: False)

    checks = run_doctor_checks(project_file)
    docker_cli_check = next(check for check in checks if check.name == "docker CLI")
    docker_socket_check = next(check for check in checks if check.name == "docker socket")

    assert docker_cli_check.status == "fail"
    assert "Docker CLI missing" in docker_cli_check.details
    assert docker_socket_check.status == "fail"
    assert "/var/run/docker.sock" in docker_socket_check.details
