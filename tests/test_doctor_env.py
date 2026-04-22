from __future__ import annotations

from pathlib import Path

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
