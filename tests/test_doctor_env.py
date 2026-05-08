from __future__ import annotations

from pathlib import Path

import pytest

from cascade.doctor import run_doctor_checks


def test_doctor_reports_github_and_model_env_presence_without_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
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

    monkeypatch.setenv("GH_TOKEN", "ghp_secret_value_should_not_be_printed")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-value-should-not-be-printed")

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

    checks = run_doctor_checks(project_file)
    gh_env = next(check for check in checks if check.name == "GitHub token env")
    model_env = next(check for check in checks if check.name == "model token env")

    assert gh_env.status == "ok"
    assert gh_env.details == "GH_TOKEN/GITHUB_TOKEN present"
    assert "secret" not in gh_env.details.lower()
    assert model_env.status == "ok"
    assert model_env.details == "At least one model API token is present"
    assert "secret" not in model_env.details.lower()


def test_doctor_warns_when_github_and_model_env_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
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

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

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

    checks = run_doctor_checks(project_file)
    gh_env = next(check for check in checks if check.name == "GitHub token env")
    model_env = next(check for check in checks if check.name == "model token env")

    assert gh_env.status == "warn"
    assert gh_env.details == "GH_TOKEN and GITHUB_TOKEN are missing from environment"
    assert model_env.status == "warn"
    assert model_env.details == "No model API token found in environment"


def _make_project_file(tmp_path: Path) -> Path:
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


def test_doctor_checks_docker_buildx_in_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _make_project_file(tmp_path)

    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    monkeypatch.setenv("COMPOSE_DOCKER_CLI_BUILD", "1")

    def _which(name: str) -> str | None:
        if name in ("gh", "opencode", "docker"):
            return f"/usr/bin/{name}"
        return None

    class _OK:
        returncode = 0
        stdout = "Docker Compose version v2.35.1"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _OK())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda *args, **kwargs: True)

    checks = run_doctor_checks(project_file)
    compose_check = next((c for c in checks if c.name == "docker compose version"), None)
    buildx_check = next((c for c in checks if c.name == "docker buildx version"), None)
    buildkit_check = next((c for c in checks if c.name == "DOCKER_BUILDKIT"), None)
    compose_buildkit_check = next((c for c in checks if c.name == "COMPOSE_DOCKER_CLI_BUILD"), None)

    assert compose_check is not None
    assert compose_check.status == "ok"
    assert buildx_check is not None
    assert buildx_check.status == "ok"
    assert buildkit_check is not None
    assert buildkit_check.status == "ok"
    assert compose_buildkit_check is not None
    assert compose_buildkit_check.status == "ok"


def test_doctor_warns_when_buildkit_env_missing_in_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _make_project_file(tmp_path)

    monkeypatch.delenv("DOCKER_BUILDKIT", raising=False)
    monkeypatch.delenv("COMPOSE_DOCKER_CLI_BUILD", raising=False)

    def _which(name: str) -> str | None:
        if name in ("gh", "opencode", "docker"):
            return f"/usr/bin/{name}"
        return None

    class _OK:
        returncode = 0
        stdout = "Docker Buildx version v0.21.0"

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _OK())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda *args, **kwargs: True)

    checks = run_doctor_checks(project_file)
    buildkit_check = next((c for c in checks if c.name == "DOCKER_BUILDKIT"), None)
    compose_buildkit_check = next((c for c in checks if c.name == "COMPOSE_DOCKER_CLI_BUILD"), None)

    assert buildkit_check is not None
    assert buildkit_check.status == "warn"
    assert "DOCKER_BUILDKIT=1" in buildkit_check.details
    assert compose_buildkit_check is not None
    assert compose_buildkit_check.status == "warn"
    assert "COMPOSE_DOCKER_CLI_BUILD=1" in compose_buildkit_check.details


def test_doctor_fails_when_buildx_missing_in_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _make_project_file(tmp_path)

    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    monkeypatch.setenv("COMPOSE_DOCKER_CLI_BUILD", "1")

    def _which(name: str) -> str | None:
        if name in ("gh", "opencode", "docker"):
            return f"/usr/bin/{name}"
        return None

    class _Fail:
        returncode = 1
        stdout = "docker: 'buildx' is not a docker command."

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", lambda *args, **kwargs: _Fail())
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda *args, **kwargs: True)

    checks = run_doctor_checks(project_file)
    buildx_check = next((c for c in checks if c.name == "docker buildx version"), None)

    assert buildx_check is not None
    assert buildx_check.status == "fail"
    assert "rebuild" in buildx_check.details.lower()


def test_doctor_warns_on_docker_desktop_workspace_path_parity_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  workspace_root: /workspace
  repo_root: /workspace/jungle
  worktree_root: /workspace/jungle-worktrees
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

    def _which(name: str) -> str | None:
        if name in ("gh", "opencode", "docker"):
            return f"/usr/bin/{name}"
        return None

    class _Completed:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _run(args, **kwargs):
        joined = " ".join(args)
        if args[:2] == ["docker", "info"]:
            return _Completed(0, "Operating System: Docker Desktop")
        if args[:3] == ["gh", "auth", "status"]:
            return _Completed(0, "authenticated")
        if "remote.origin.url" in joined:
            return _Completed(0, "https://github.com/pinestraw/jungle.git")
        return _Completed(0, "ok")

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", _run)
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda *args, **kwargs: True)

    checks = run_doctor_checks(project_file)
    parity_check = next((c for c in checks if c.name == "docker host-path parity"), None)

    assert parity_check is not None
    assert parity_check.status == "warn"
    assert (
        parity_check.details
        == "Host Docker bind mounts may fail; prefer host-native Cascade or configure host-path parity."
    )


def test_doctor_skips_parity_warning_without_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = _make_project_file(tmp_path)

    def _which(name: str) -> str | None:
        if name in ("gh", "opencode", "docker"):
            return f"/usr/bin/{name}"
        return None

    class _Completed:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _run(args, **kwargs):
        joined = " ".join(args)
        if args[:2] == ["docker", "info"]:
            return _Completed(0, "Operating System: Docker Desktop")
        if args[:3] == ["gh", "auth", "status"]:
            return _Completed(0, "authenticated")
        if "remote.origin.url" in joined:
            return _Completed(0, "https://github.com/pinestraw/jungle.git")
        return _Completed(0, "ok")

    monkeypatch.setattr("cascade.doctor.shutil.which", _which)
    monkeypatch.setattr("cascade.doctor.subprocess.run", _run)
    monkeypatch.setattr("cascade.doctor._running_in_docker", lambda: True)
    monkeypatch.setattr("cascade.doctor.Path.exists", lambda self: True)
    monkeypatch.setattr("cascade.doctor.os.access", lambda *args, **kwargs: True)

    checks = run_doctor_checks(project_file)
    parity_check = next((c for c in checks if c.name == "docker host-path parity"), None)

    assert parity_check is None
