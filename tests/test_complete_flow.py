from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade import gate_fix as gate_fix_module
from cascade.cli import app


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _write_project_file(tmp_path: Path) -> Path:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / 'repo'}
  worktree_root: {tmp_path / 'worktrees'}
commands:
  create_worktree: echo create
  preflight: {sys.executable} .cascade_preflight.py
  done: {sys.executable} .cascade_done.py
  propagate: {sys.executable} .cascade_propagate.py
branches:
  active_branch: staging
  agent_branch_template: agent/{{agent}}/{{slug}}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
  profiles:
    cheap_coder:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      use_for: [fix]
    debugger:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      use_for: [debug]
    executor:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      use_for: [execute]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def _write_agent_state(
    tmp_path: Path,
    *,
    project_file: Path,
    worktree: Path,
    run_dir: Path,
    slug: str,
    title: str,
    state: str = "claimed",
) -> Path:
    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "project": "jungle",
                "agent": "a1",
                "issue": 101,
                "title": title,
                "slug": slug,
                "engine": "opencode",
                "model": "openrouter/z-ai/glm-4.7-flash",
                "state": state,
                "worktree": str(worktree),
                "run_dir": str(run_dir),
                "project_file": str(project_file),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return state_path


def _write_mandate_metadata(worktree: Path, run_dir: Path, *, slug: str, title: str) -> Path:
    metadata_path = worktree / ".github" / "mandates" / f"{slug}.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "slug": slug,
                "title": title,
                "status": "in_progress",
                "repo": "jungle",
                "agent": "a1",
                "agent_branch": f"agent/a1/{slug}",
                "active_branch": "staging",
                "worktree_path": str(worktree.resolve()),
                "canonical_mandate": str((run_dir / "mandate.md").resolve()),
                "mandate_id": "JNG-04242026-001",
                "github_project_item_id": "PVT_jungle_complete_flow",
                "file_scope": ["src/**", ".github/mandates/**"],
                "commits": [],
                "precommit_failures": 0,
                "created_at": "2026-04-24T00:00:00Z",
                "updated_at": "2026-04-24T00:00:00Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return metadata_path


def _write_common_scripts(worktree: Path, event_log: Path, preflight_body: str) -> None:
    worktree.joinpath(".cascade_preflight.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "",
                f"EVENT_LOG = Path({str(event_log)!r})",
                'with EVENT_LOG.open("a", encoding="utf-8") as handle:',
                '    handle.write("preflight\\n")',
                "",
                preflight_body.strip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    worktree.joinpath(".cascade_done.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(event_log)!r}).open('a', encoding='utf-8').write('done\\n')",
                "",
            ]
        ),
        encoding="utf-8",
    )
    worktree.joinpath(".cascade_propagate.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(event_log)!r}).open('a', encoding='utf-8').write('propagate\\n')",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _setup_complete_agent(
    tmp_path: Path,
    *,
    slug: str,
    title: str,
    preflight_body: str,
    initial_module: str = '"""Baseline docstring"""\nVALUE = 1\n',
) -> dict[str, Path | str]:
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktrees" / f"a1-{slug}"
    worktree.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mandate.md").write_text("# Mandate\n\nComplete flow test\n", encoding="utf-8")

    event_log = tmp_path / "events.log"
    hook_log = tmp_path / "hook.log"
    project_file = _write_project_file(tmp_path)
    _write_common_scripts(worktree, event_log, preflight_body)

    module_path = worktree / "src" / "module.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(initial_module, encoding="utf-8")

    audit_log = worktree / ".github" / "mandates" / "audit.log"
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.write_text("created\n", encoding="utf-8")
    metadata_path = _write_mandate_metadata(worktree, run_dir, slug=slug, title=title)

    _git(worktree, "init")
    _git(worktree, "config", "user.email", "test@example.com")
    _git(worktree, "config", "user.name", "Test User")
    _git(worktree, "checkout", "-b", f"agent/a1/{slug}")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "baseline")

    state_path = _write_agent_state(
        tmp_path,
        project_file=project_file,
        worktree=worktree,
        run_dir=run_dir,
        slug=slug,
        title=title,
    )
    return {
        "project_file": project_file,
        "worktree": worktree,
        "run_dir": run_dir,
        "state_path": state_path,
        "metadata_path": metadata_path,
        "module_path": module_path,
        "event_log": event_log,
        "hook_log": hook_log,
    }


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _complete_payload(tmp_path: Path) -> dict[str, object]:
    return _read_json(tmp_path / "state" / "jungle" / "runs" / "a1" / "complete_loop.json")


def _agent_state(tmp_path: Path) -> dict[str, object]:
    return _read_json(tmp_path / "state" / "jungle" / "agents" / "a1.json")


def _invoke_complete(tmp_path: Path) -> object:
    runner = CliRunner()
    return runner.invoke(app, ["complete", "a1", "--project", "jungle", "--yes"])


def _invoke_complete_v2(tmp_path: Path, *, yes: bool = True) -> object:
    runner = CliRunner()
    args = ["complete-v2", "a1", "--project", "jungle"]
    if yes:
        args.append("--yes")
    return runner.invoke(app, args)


def test_complete_happy_path_commits_finishes_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="happy-path",
        title="Happy Path",
        preflight_body='print("preflight ok")',
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    module_path = Path(str(ctx["module_path"]))
    module_path.write_text('"""Updated docstring"""\nVALUE = 2\n', encoding="utf-8")

    result = _invoke_complete(tmp_path)

    assert result.exit_code == 0, result.output
    assert _agent_state(tmp_path)["state"] == "closed"
    assert _complete_payload(tmp_path)["status"] == "completed"
    assert _complete_payload(tmp_path)["completed_phases"] == [
        "closeout_prep_stage",
        "mandate_commit",
        "post_commit_preflight",
        "finish",
        "closeout",
    ]
    assert _read_lines(Path(str(ctx["event_log"]))) == ["preflight", "done", "propagate"]
    assert "src/module.py" in _git(Path(str(ctx["worktree"])), "show", "--name-only", "--format=", "HEAD")
    assert _git(Path(str(ctx["worktree"])), "show", "HEAD:src/module.py") == '"""Updated docstring"""\nVALUE = 2'
    assert _git(Path(str(ctx["worktree"])), "rev-list", "--count", "HEAD") == "2"
    assert not _git(Path(str(ctx["worktree"])), "status", "--porcelain")


def test_complete_preflight_repair_loop_runs_to_closeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="repair-loop",
        title="Repair Loop",
        preflight_body="""
content = Path("src/module.py").read_text(encoding="utf-8")
if "MISSING_DOCSTRING" in content:
    print("backend-docstring")
    print("src/module.py:1: D103 Missing docstring in public module")
    raise SystemExit(1)
print("preflight ok")
""",
    initial_module="MISSING_DOCSTRING\nVALUE = 2\n",
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    module_path = Path(str(ctx["module_path"]))
    fix_calls = {"count": 0}

    def _run_model_fix_attempt(**kwargs: object) -> tuple[bool, str, float, str | None]:
        fix_calls["count"] += 1
        Path(str(kwargs["agent_state"]["worktree"]))
        module_path.write_text('"""Recovered docstring"""\nVALUE = 2\n', encoding="utf-8")
        return True, "fixed docstring", 0.0, "cheap_coder"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _run_model_fix_attempt)

    result = _invoke_complete(tmp_path)

    assert result.exit_code == 0, result.output
    assert fix_calls["count"] == 1
    assert _agent_state(tmp_path)["state"] == "closed"
    assert _read_lines(Path(str(ctx["event_log"]))).count("preflight") == 3
    assert _read_lines(Path(str(ctx["event_log"])))[-2:] == ["done", "propagate"]
    assert _complete_payload(tmp_path)["status"] == "completed"


def test_complete_restages_gate_fix_edits_before_commit_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="commit-gate-regression",
        title="Commit Gate Regression",
        preflight_body='print("preflight ok")',
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    worktree = Path(str(ctx["worktree"]))
    module_path = Path(str(ctx["module_path"]))
    hook_log = Path(str(ctx["hook_log"]))

    module_path.write_text("VALUE = 2\n", encoding="utf-8")
    _git(worktree, "add", "src/module.py")

    hook_path = worktree / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'content="$(git show :src/module.py)"',
                "case \"$content\" in",
                "  '\"\"\"'*)",
                f"    printf 'yes\\n' >> {str(hook_log)!r}",
                "    ;;",
                "  *)",
                f"    printf 'no\\n' >> {str(hook_log)!r}",
                '     echo "backend-docstring"',
                '     echo "src/module.py:1: D103 Missing docstring in public module"',
                "     exit 1",
                "     ;;",
                "esac",
                "",
            ]
        ),
        encoding="utf-8",
    )
    hook_path.chmod(0o755)

    def _stream_openrouter_request(model: str, messages: object, config: object, run_dir: Path, attempt: int):
        response = "```json\n" + json.dumps(
            {
                "summary": "Restore docstring",
                "edits": [
                    {
                        "path": "src/module.py",
                        "content": '"""Restored docstring"""\nVALUE = 2\n',
                    }
                ],
            }
        ) + "\n```"
        return response, {"model": model, "attempt": attempt}, {"status_code": 200}

    monkeypatch.setattr(gate_fix_module, "stream_openrouter_request", _stream_openrouter_request)

    result = _invoke_complete(tmp_path)

    assert result.exit_code == 0, result.output
    assert _read_lines(hook_log) == ["no", "no", "yes"]
    assert _git(worktree, "show", "HEAD:src/module.py") == '"""Restored docstring"""\nVALUE = 2'
    assert _complete_payload(tmp_path)["status"] == "completed"


def test_complete_passes_full_visible_failure_batch_to_model_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="failure-batch",
        title="Failure Batch",
        preflight_body="""
content = Path("src/module.py").read_text(encoding="utf-8")
lines: list[str] = []
if "MISSING_DOCSTRING" in content:
    lines.extend([
        "backend-docstring",
        "src/module.py:1: D103 Missing docstring in public module",
    ])
if "UNUSED_IMPORT" in content:
    lines.extend([
        "ruff",
        "src/module.py:2: F401 `os` imported but unused",
    ])
if lines:
    print("\\n".join(lines))
    raise SystemExit(1)
print("preflight ok")
""",
    initial_module="import os\nMISSING_DOCSTRING\nUNUSED_IMPORT\n",
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))
    monkeypatch.setattr(
        cli_module,
        "classify_gate_failure",
        lambda log_text: {
            "detected": True,
            "hook": "backend-docstring",
            "category": "docstring",
            "model_recommended": True,
            "suggested_no_model_action": "",
        },
    )

    module_path = Path(str(ctx["module_path"]))
    seen = {"value": False}

    def _run_model_fix_attempt(**kwargs: object) -> tuple[bool, str, float, str | None]:
        log_tail = str(kwargs["log_tail"])
        assert "D103 Missing docstring" in log_tail
        assert "F401 `os` imported but unused" in log_tail
        seen["value"] = True
        module_path.write_text('"""Batch fixed"""\nVALUE = 3\n', encoding="utf-8")
        return True, "fixed failure batch", 0.0, "cheap_coder"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _run_model_fix_attempt)

    result = _invoke_complete(tmp_path)

    assert result.exit_code == 0, result.output
    assert seen["value"] is True
    assert _agent_state(tmp_path)["state"] == "closed"
    assert _complete_payload(tmp_path)["status"] == "completed"


def test_complete_stops_for_suspicious_extras_without_git_add_dot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="suspicious-extras",
        title="Suspicious Extras",
        preflight_body='print("preflight ok")',
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    worktree = Path(str(ctx["worktree"]))
    module_path = Path(str(ctx["module_path"]))
    module_path.write_text('"""Updated docstring"""\nVALUE = 4\n', encoding="utf-8")
    (worktree / "notes.txt").write_text("unexpected extra\n", encoding="utf-8")

    original_run_command = cli_module.run_command
    commands: list[str] = []

    def _run_command(cmd: str, cwd: Path | None = None):
        commands.append(cmd)
        return original_run_command(cmd, cwd=cwd)

    monkeypatch.setattr(cli_module, "run_command", _run_command)

    result = _invoke_complete(tmp_path)

    assert result.exit_code != 0
    assert not any(cmd.strip() == "git add ." for cmd in commands)
    assert _complete_payload(tmp_path)["status"] == "needs_human"
    assert _complete_payload(tmp_path)["current_phase"] == "closeout_prep_stage"
    assert "Suspicious extras detected" in str(_complete_payload(tmp_path).get("stop_reason") or "")
    assert _git(worktree, "rev-list", "--count", "HEAD") == "1"
    assert "notes.txt" not in _git(worktree, "diff", "--cached", "--name-only")
    assert "?? notes.txt" in _git(worktree, "status", "--porcelain")


def test_complete_preflight_fix_does_not_stage_before_commit_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="non-commit-safety",
        title="Non Commit Safety",
        preflight_body="""
content = Path("src/module.py").read_text(encoding="utf-8")
if "MISSING_DOCSTRING" in content:
    print("backend-docstring")
    print("src/module.py:1: D103 Missing docstring in public module")
    raise SystemExit(1)
print("preflight ok")
""",
    initial_module="MISSING_DOCSTRING\nVALUE = 5\n",
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    worktree = Path(str(ctx["worktree"]))
    module_path = Path(str(ctx["module_path"]))
    staged_checks = {"before": "", "after": ""}

    def _run_model_fix_attempt(**kwargs: object) -> tuple[bool, str, float, str | None]:
        staged_checks["before"] = _git(worktree, "diff", "--cached", "--name-only")
        module_path.write_text('"""Safe fix"""\nVALUE = 5\n', encoding="utf-8")
        staged_checks["after"] = _git(worktree, "diff", "--cached", "--name-only")
        return True, "fixed without staging", 0.0, "cheap_coder"

    monkeypatch.setattr(cli_module, "_run_model_fix_attempt", _run_model_fix_attempt)

    result = _invoke_complete(tmp_path)

    assert result.exit_code == 0, result.output
    assert staged_checks == {"before": "", "after": ""}
    assert _agent_state(tmp_path)["state"] == "closed"


def test_complete_resumes_from_recorded_phase_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="resume-flow",
        title="Resume Flow",
        preflight_body='print("preflight ok")',
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    module_path = Path(str(ctx["module_path"]))
    module_path.write_text('"""Resume change"""\nVALUE = 6\n', encoding="utf-8")

    original_execute = cli_module._execute_closeout_prep_flow

    def _explode_once(**kwargs: object):
        raise RuntimeError("synthetic commit interruption")

    monkeypatch.setattr(cli_module, "_execute_closeout_prep_flow", _explode_once)
    first_result = _invoke_complete(tmp_path)

    assert first_result.exit_code != 0
    assert _complete_payload(tmp_path)["status"] == "failed"
    assert _complete_payload(tmp_path)["current_phase"] == "mandate_commit"
    assert _complete_payload(tmp_path)["next_phase"] == "mandate_commit"

    monkeypatch.setattr(cli_module, "_execute_closeout_prep_flow", original_execute)
    second_result = _invoke_complete(tmp_path)

    assert second_result.exit_code == 0, second_result.output
    assert _read_lines(Path(str(ctx["event_log"]))).count("preflight") == 1
    assert _complete_payload(tmp_path)["status"] == "completed"
    assert _agent_state(tmp_path)["state"] == "closed"


def test_complete_transitions_dirty_file_preflight_failure_to_commit_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="dirty-file-transition",
        title="Dirty File Transition",
        preflight_body='''
marker = EVENT_LOG.parent / "preflight_failed_once"
if not marker.exists():
    marker.write_text("1\\n", encoding="utf-8")
    Path("src/module.py").write_text('"""Post implementation update"""\\nVALUE = 7\\n', encoding="utf-8")
    print("[mandate] ERROR: Unexpected dirty file while closing mandate: api/serializers/inventory.py")
    print("dirty_file_commit_required")
    raise SystemExit(2)
print("preflight ok")
    ''',
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    result = _invoke_complete(tmp_path)

    worktree = Path(str(ctx["worktree"]))
    payload = _complete_payload(tmp_path)
    assert result.exit_code == 0, result.output
    assert payload["status"] == "completed"
    assert payload.get("last_transition_reason") == "mandate_commit_required"
    assert payload.get("mandate_commit_required_source") == "preflight"
    assert "closeout_prep_stage" in payload["completed_phases"]
    assert "mandate_commit" in payload["completed_phases"]
    assert _git(worktree, "rev-list", "--count", "HEAD") == "2"
    assert _git(worktree, "show", "HEAD:src/module.py") == '"""Post implementation update"""\nVALUE = 7'
    assert _read_lines(Path(str(ctx["event_log"]))).count("preflight") >= 2
    assert _read_lines(Path(str(ctx["event_log"])))[-2:] == ["done", "propagate"]


def test_complete_v2_happy_path_commits_finishes_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _setup_complete_agent(
        tmp_path,
        slug="happy-path-v2",
        title="Happy Path V2",
        preflight_body='print("preflight ok")',
    )
    monkeypatch.setattr(cli_module, "supports_non_interactive_run", lambda: (True, None))

    module_path = Path(str(ctx["module_path"]))
    module_path.write_text('"""Updated docstring v2"""\nVALUE = 8\n', encoding="utf-8")

    result = _invoke_complete_v2(tmp_path)

    assert result.exit_code == 0, result.output
    assert _agent_state(tmp_path)["state"] == "closed"
    assert _complete_payload(tmp_path)["status"] == "completed"
    assert _read_lines(Path(str(ctx["event_log"]))) == ["preflight", "done", "propagate"]


def test_complete_v2_requires_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_complete_agent(
        tmp_path,
        slug="requires-yes-v2",
        title="Requires Yes V2",
        preflight_body='print("preflight ok")',
    )

    result = _invoke_complete_v2(tmp_path, yes=False)

    assert result.exit_code != 0
    assert "Re-run with --yes" in result.output