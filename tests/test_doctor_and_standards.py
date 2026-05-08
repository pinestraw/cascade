from __future__ import annotations

from pathlib import Path

import pytest

from cascade.cli import _opencode_external_directory_warning
from cascade.config import CommandsConfig, GithubConfig, PathsConfig, ProjectConfig, WorkspaceLinkConfig
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


def test_doctor_checks_workspace_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-worktrees").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jungle-secrets").mkdir(parents=True, exist_ok=True)

    project_file = tmp_path / "project_workspace_links.yaml"
    project_file.write_text(
        """
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: "."
  repo_root: "repo"
  worktree_root: "jungle-worktrees"
  secrets_root: "jungle-secrets"
workspace_links:
  - link: "{worktree_root}/jungle-secrets"
    target: "{secrets_root}"
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

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return "/usr/local/bin/opencode"
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: False)

    checks = run_doctor_checks(project_file)
    workspace_link_check = next(check for check in checks if check.name == "workspace_link[1]")
    assert workspace_link_check.status == "fail"
    assert "Link path is missing" in workspace_link_check.details


def _doctor_workspace_link_project_file(
    tmp_path: Path,
    *,
    workspace_root: Path,
    link_path: Path,
    target_path: Path,
) -> Path:
    project_file = tmp_path / "project_workspace_links_cases.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: "{workspace_root}"
  repo_root: "{tmp_path / 'repo'}"
  worktree_root: "{tmp_path / 'jungle-worktrees'}"
  secrets_root: "{tmp_path / 'jungle-secrets'}"
workspace_links:
  - link: "{link_path}"
    target: "{target_path}"
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


def _mock_doctor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return "/usr/local/bin/opencode"
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: False)


def test_doctor_workspace_link_inside_points_inside_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace_root = tmp_path / "instica-workspace"
    (workspace_root / "repo").mkdir(parents=True, exist_ok=True)
    (workspace_root / "jungle-worktrees").mkdir(parents=True, exist_ok=True)
    (workspace_root / "jungle-secrets").mkdir(parents=True, exist_ok=True)

    link_path = workspace_root / "jungle-worktrees" / "jungle-secrets"
    target_path = workspace_root / "jungle-secrets"
    link_path.symlink_to(target_path)

    project_file = _doctor_workspace_link_project_file(
        tmp_path,
        workspace_root=workspace_root,
        link_path=link_path,
        target_path=target_path,
    )
    _mock_doctor_env(monkeypatch)

    checks = run_doctor_checks(project_file)
    workspace_link_check = next(check for check in checks if check.name == "workspace_link[1]")
    assert workspace_link_check.status == "ok"


def test_doctor_warns_on_docker_era_agent_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    project_file = _write_project_file(tmp_path)

    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        """
{
  "project": "jungle",
  "agent": "a1",
  "project_file": "/workspace/cascade/examples/jungle.yaml",
  "worktree": "/workspace/jungle-worktrees/a1-test"
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _which(name: str):
        if name in {"gh", "opencode"}:
            return f"/usr/bin/{name}"
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: False)

    checks = run_doctor_checks(project_file)
    stale = next(check for check in checks if check.name == "docker-era-agent-state")
    assert stale.status == "warn"


def test_doctor_workspace_link_inside_points_outside_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace_root = tmp_path / "instica-workspace"
    outside_root = tmp_path / "outside"
    (workspace_root / "repo").mkdir(parents=True, exist_ok=True)
    (workspace_root / "jungle-worktrees").mkdir(parents=True, exist_ok=True)
    (workspace_root / "jungle-secrets").mkdir(parents=True, exist_ok=True)
    outside_root.mkdir(parents=True, exist_ok=True)

    link_path = workspace_root / "jungle-worktrees" / "jungle-secrets"
    target_path = outside_root / "jungle-secrets"
    target_path.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(target_path)

    project_file = _doctor_workspace_link_project_file(
        tmp_path,
        workspace_root=workspace_root,
        link_path=link_path,
        target_path=target_path,
    )
    _mock_doctor_env(monkeypatch)

    checks = run_doctor_checks(project_file)
    workspace_link_check = next(check for check in checks if check.name == "workspace_link[1]")
    assert workspace_link_check.status == "fail"
    assert "Target path escapes workspace_root" in workspace_link_check.details


def test_doctor_workspace_link_path_outside_workspace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace_root = tmp_path / "instica-workspace"
    outside_root = tmp_path / "outside"
    (workspace_root / "repo").mkdir(parents=True, exist_ok=True)
    (workspace_root / "jungle-worktrees").mkdir(parents=True, exist_ok=True)
    (workspace_root / "jungle-secrets").mkdir(parents=True, exist_ok=True)
    outside_root.mkdir(parents=True, exist_ok=True)

    link_path = outside_root / "jungle-worktrees" / "jungle-secrets"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    target_path = workspace_root / "jungle-secrets"
    link_path.symlink_to(target_path)

    project_file = _doctor_workspace_link_project_file(
        tmp_path,
        workspace_root=workspace_root,
        link_path=link_path,
        target_path=target_path,
    )
    _mock_doctor_env(monkeypatch)

    checks = run_doctor_checks(project_file)
    workspace_link_check = next(check for check in checks if check.name == "workspace_link[1]")
    assert workspace_link_check.status == "fail"
    assert "Link path escapes workspace_root" in workspace_link_check.details


def test_doctor_reports_init_mandate_target_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "worktrees").mkdir()
    (repo / "Makefile").write_text("mandate-start:\n\t@echo ok\n", encoding="utf-8")

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
    mandate_start: make mandate-start MANDATE_SLUG={slug}
models:
    default:
        provider: openrouter
        model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return "/usr/local/bin/opencode"
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: False)

    checks = run_doctor_checks(project_file)
    init_check = next(check for check in checks if check.name == "mandate_start target")

    assert init_check.status == "ok"
    assert "mandate-start" in init_check.details


def test_doctor_warns_when_init_mandate_target_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "worktrees").mkdir()
    (repo / "Makefile").write_text("other-target:\n\t@echo ok\n", encoding="utf-8")

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
    start_mandate: make mandate-start MANDATE_SLUG={slug}
models:
    default:
        provider: openrouter
        model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _which(name: str):
        if name == "gh":
            return "/usr/bin/gh"
        if name == "opencode":
            return "/usr/local/bin/opencode"
        return None

    class _Completed:
        returncode = 0
        stdout = "authenticated"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Completed())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: False)

    checks = run_doctor_checks(project_file)
    init_check = next(check for check in checks if check.name == "mandate_start target")

    assert init_check.status == "warn"
    assert "but it was not found" in init_check.details


# ---------------------------------------------------------------------------
# _opencode_external_directory_warning tests
# ---------------------------------------------------------------------------


def _make_project_with_workspace_links(tmp_path: Path) -> ProjectConfig:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    return ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(
            repo_root=workspace_root / "jungle",
            worktree_root=workspace_root / "worktrees",
            workspace_root=workspace_root,
        ),
        commands=CommandsConfig(create_worktree="echo create"),
        workspace_links=[WorkspaceLinkConfig(link="worktrees/{agent}/secrets", target="secrets")],
    )


def test_opencode_external_directory_warning_no_workspace_links() -> None:
    """No workspace_links → no warning regardless of opencode.json."""
    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=Path("/tmp/repo"), worktree_root=Path("/tmp/worktrees")),
        commands=CommandsConfig(create_worktree="echo create"),
    )
    assert _opencode_external_directory_warning(project) is None


def test_opencode_external_directory_warning_missing_opencode_json(tmp_path: Path) -> None:
    """workspace_links present but no opencode.json → warning emitted."""
    project = _make_project_with_workspace_links(tmp_path)
    warning = _opencode_external_directory_warning(project)
    assert warning is not None
    assert "opencode.json" in warning
    assert "external_directory" in warning


def test_opencode_external_directory_warning_present_but_no_external_directory_key(tmp_path: Path) -> None:
    """opencode.json exists but lacks permission.external_directory → warning emitted."""
    project = _make_project_with_workspace_links(tmp_path)
    workspace_root = project.paths.workspace_root
    assert workspace_root is not None
    (workspace_root / "opencode.json").write_text('{"permission": {}}', encoding="utf-8")

    warning = _opencode_external_directory_warning(project)
    assert warning is not None
    assert "external_directory" in warning


def test_opencode_external_directory_warning_suppressed_when_config_present(tmp_path: Path) -> None:
    """opencode.json with permission.external_directory → no warning."""
    project = _make_project_with_workspace_links(tmp_path)
    workspace_root = project.paths.workspace_root
    assert workspace_root is not None
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"external_directory": {"~/worktrees/**": "allow"}},
    }
    import json

    (workspace_root / "opencode.json").write_text(json.dumps(config), encoding="utf-8")

    assert _opencode_external_directory_warning(project) is None


def test_opencode_external_directory_warning_no_workspace_root_skips_check(tmp_path: Path) -> None:
    """workspace_links present but workspace_root not configured → no warning (can't locate file)."""
    project = ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "worktrees"),
        commands=CommandsConfig(create_worktree="echo create"),
        workspace_links=[WorkspaceLinkConfig(link="worktrees/{agent}/secrets", target="secrets")],
    )
    assert _opencode_external_directory_warning(project) is None
