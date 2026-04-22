from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module


def write_agent_state(state_path: Path, worktree: Path, run_dir: Path, project_file: Path) -> None:
    state_path.write_text(
        """
{
  "project": "jungle",
  "agent": "oc1",
  "issue": 45,
  "title": "Daily Digest",
  "slug": "daily-digest",
  "engine": "opencode",
  "model": "openrouter/z-ai/glm-4.7-flash",
  "state": "claimed",
  "worktree": "WT_PLACEHOLDER",
  "run_dir": "RUN_PLACEHOLDER",
  "project_file": "PROJECT_FILE_PLACEHOLDER"
}
""".replace("WT_PLACEHOLDER", str(worktree))
        .replace("RUN_PLACEHOLDER", str(run_dir))
        .replace("PROJECT_FILE_PLACEHOLDER", str(project_file))
        .strip()
        + "\n",
        encoding="utf-8",
    )


def write_project_file(path: Path, worktree_root: Path) -> None:
    path.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: ./repo
  worktree_root: {worktree_root}
commands:
  create_worktree: echo create
  preflight: echo preflight-ok
instructions:
  files:
    - COPILOT.md
    - .github/copilot-instructions.md
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_note_appends_without_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project = "jungle"
    agent = "oc1"
    worktree = tmp_path / "worktrees" / "oc1-daily-digest"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)
    state_path = tmp_path / "state" / project / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True)
    project_file = tmp_path / "project.yaml"
    write_project_file(project_file, tmp_path / "worktrees")
    write_agent_state(state_path, worktree, run_dir, project_file)

    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: (_ for _ in ()).throw(RuntimeError("should not call")))

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["note", agent, "--project", project, "--message", "Deterministic note"]) 

    assert result.exit_code == 0
    assert "Deterministic note" in (run_dir / "decisions.md").read_text(encoding="utf-8")


def test_context_generation_uses_deterministic_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project = "jungle"
    agent = "oc1"
    worktree = tmp_path / "worktrees" / "oc1-daily-digest"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)
    state_path = tmp_path / "state" / project / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True)
    project_file = tmp_path / "project.yaml"
    write_project_file(project_file, tmp_path / "worktrees")
    write_agent_state(state_path, worktree, run_dir, project_file)
    (run_dir / "mandate.md").write_text("Implement mandate", encoding="utf-8")

    monkeypatch.setattr(cli_module, "get_git_status", lambda wt: "M foo.py")
    monkeypatch.setattr(cli_module, "get_git_diff_stat", lambda wt: "1 file changed")
    monkeypatch.setattr(cli_module, "get_current_branch", lambda wt: "agent/oc1/daily-digest")
    monkeypatch.setattr(cli_module, "get_git_diff_names", lambda wt: "foo.py")

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["context", agent, "--project", project, "--include-diff", "--print"])

    assert result.exit_code == 0
    output = result.output
    assert "Agent Metadata" in output
    assert "COPILOT.md" in output
    assert "Issue: #45" in output
    assert "M foo.py" in output
    assert "Warnings" in output
    assert "Instruction file not found" in output
    assert "foo.py" in (run_dir / "context.md").read_text(encoding="utf-8")


def test_diff_command_uses_git_helpers_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project = "jungle"
    agent = "oc1"
    worktree = tmp_path / "worktrees" / "oc1-daily-digest"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)
    state_path = tmp_path / "state" / project / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True)
    project_file = tmp_path / "project.yaml"
    write_project_file(project_file, tmp_path / "worktrees")
    write_agent_state(state_path, worktree, run_dir, project_file)

    called = {"status": False, "stat": False, "names": False}

    def _status(_wt: Path) -> str:
        called["status"] = True
        return "M foo.py"

    def _stat(_wt: Path) -> str:
        called["stat"] = True
        return "1 file changed"

    def _names(_wt: Path) -> str:
        called["names"] = True
        return "foo.py"

    monkeypatch.setattr(cli_module, "get_git_status", _status)
    monkeypatch.setattr(cli_module, "get_git_diff_stat", _stat)
    monkeypatch.setattr(cli_module, "get_git_diff_names", _names)
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: (_ for _ in ()).throw(RuntimeError("should not call")))

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["diff", agent, "--project", project])

    assert result.exit_code == 0
    assert all(called.values())


def test_capabilities_output_has_categories() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["capabilities"])

    assert result.exit_code == 0
    assert "deterministic" in result.output
    assert "model-backed" in result.output
    assert "planned" in result.output


def test_claim_uses_configured_create_worktree_without_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
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
  create_worktree: custom-create --agent={agent} --slug={slug}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "worktrees").mkdir()

    called: dict[str, str] = {}

    monkeypatch.setattr(cli_module, "fetch_issue", lambda owner, repo, issue: {"title": "Issue Title", "body": "Body", "number": issue})

    def _run_command(cmd: str, cwd: Path | None = None):
        called["cmd"] = cmd
        class _Result:
            stdout = ""
        return _Result()

    monkeypatch.setattr(cli_module, "run_command", _run_command)
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: (_ for _ in ()).throw(RuntimeError("should not call")))

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["claim", "--project-file", str(project_file), "--issue", "45", "--agent", "oc1", "--model", "openrouter/z-ai/glm-4.7-flash"])

    assert result.exit_code == 0
    assert "custom-create" in called["cmd"]


def test_preflight_uses_configured_command_without_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project = "jungle"
    agent = "oc1"
    worktree = tmp_path / "worktrees" / "oc1-daily-digest"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)
    state_path = tmp_path / "state" / project / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: ./repo
  worktree_root: {tmp_path / 'worktrees'}
commands:
  create_worktree: echo create
  preflight: custom-preflight --slug={{slug}}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    write_agent_state(state_path, worktree, run_dir, project_file)

    class FakeResult:
        returncode = 0
        stdout = "ok"

    called: dict[str, str] = {}

    def _subprocess_run(cmd, **kwargs):
        if isinstance(cmd, str) and "custom-preflight" in cmd:
            called["preflight_cmd"] = cmd
            return FakeResult()
        return FakeResult()

    monkeypatch.setattr(cli_module.subprocess, "run", _subprocess_run)
    monkeypatch.setattr(cli_module, "ensure_opencode_available", lambda: (_ for _ in ()).throw(RuntimeError("should not call")))

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["preflight", agent, "--project", project])

    assert result.exit_code == 0
    assert "custom-preflight" in called["preflight_cmd"]


def test_preflight_uses_exit_code_not_text_for_pass_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project = "jungle"
    agent = "oc1"
    worktree = tmp_path / "worktrees" / "oc1-daily-digest"
    worktree.mkdir(parents=True)
    run_dir = tmp_path / "state" / project / "runs" / agent
    run_dir.mkdir(parents=True)
    state_path = tmp_path / "state" / project / "agents" / f"{agent}.json"
    state_path.parent.mkdir(parents=True)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: ./repo
  worktree_root: {tmp_path / 'worktrees'}
commands:
  create_worktree: echo create
  preflight: custom-preflight --slug={{slug}}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    write_agent_state(state_path, worktree, run_dir, project_file)

    class FakeResult:
        returncode = 2
        stdout = "all good, trust me"

    monkeypatch.setattr(cli_module.subprocess, "run", lambda *args, **kwargs: FakeResult())

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["preflight", agent, "--project", project])

    assert result.exit_code != 0
    updated_state = cli_module.load_agent_state(project, agent)
    assert updated_state["state"] == "preflight_failed"
