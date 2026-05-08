from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cascade import cli as cli_module
from cascade import gate_fix as gate_fix_module
from cascade.cli import app
from cascade.config import ModelProfile, load_project_config, resolve_gate_fix_model_profile
from cascade.gate_fix import (
    GateFixAttempt,
    GateFixBatchMode,
    GateFixCategory,
    GateFixConfig,
    GateFixResult,
    apply_model_fixes,
    classify_failure_as_model_fixable,
    detect_unrelated_file_growth,
    is_model_fixable,
    run_gate_fix_loop,
    save_gate_fix_summary,
    stream_openrouter_request,
)
from cascade.gates import save_gate_result


def _write_project_file(tmp_path: Path, *, extra_profiles: str = "") -> Path:
    profiles_block = "  profiles: {}\n"
    if extra_profiles:
        profiles_block = f"""  profiles:\n{extra_profiles}"""

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
  preflight: make mandate-preflight MANDATE_SLUG={{slug}}
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
{profiles_block}""".strip()
        + "\n",
        encoding="utf-8",
    )
    return project_file


def _setup_gate_fix_agent(
    tmp_path: Path,
    *,
    log_content: str,
    hook: str,
    command: str = "make mandate-preflight MANDATE_SLUG=gate-fix-test",
    touched_files: list[str] | None = None,
    extra_profiles: str = "",
) -> tuple[Path, Path, Path]:
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktrees" / "a1-gate-fix"
    worktree.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "state" / "jungle" / "runs" / "a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    project_file = _write_project_file(tmp_path, extra_profiles=extra_profiles)

    state = {
        "project": "jungle",
        "agent": "a1",
        "issue": 77,
        "title": "Gate Fix",
        "slug": "gate-fix-test",
        "engine": "openrouter",
        "model": "openrouter/deepseek/deepseek-v3.2",
        "state": "claimed",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "project_file": str(project_file),
    }
    state_path = tmp_path / "state" / "jungle" / "agents" / "a1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    (run_dir / "preflight.log").write_text(log_content, encoding="utf-8")
    save_gate_result(
        run_dir,
        {
            "hook": hook,
            "command": command,
            "touched_files": touched_files or [],
        },
    )
    return project_file, worktree, run_dir


def _make_profile(model: str, *, input_cost: float = 0.0, output_cost: float = 0.0) -> ModelProfile:
    return ModelProfile(
        provider="openrouter",
        model=model,
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
        use_for=["fix"],
    )


def _stub_loop_basics(monkeypatch: pytest.MonkeyPatch, *, branch: str = "agent/a1") -> None:
    monkeypatch.setattr(
        "cascade.gate_fix.run_command",
        lambda command, cwd=None: SimpleNamespace(stdout=f"{branch}\n")
        if command == "git rev-parse --abbrev-ref HEAD"
        else SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: False)
    monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: [])
    monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
    monkeypatch.setattr("cascade.gate_fix._get_status_summary", lambda worktree: [])
    monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
    monkeypatch.setattr(
        "cascade.gate_fix._run_current_gate_probe",
        lambda **kwargs: (
            False,
            str(kwargs.get("failing_log") or "probe failure"),
            str(kwargs.get("failing_hook") or "unknown"),
            kwargs.get("fallback_category") or GateFixCategory.WORKFLOW,
            True,
            "probe-signature",
            str(kwargs.get("gate_command") or ""),
        ),
    )


def _stub_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hook: str,
    category: GateFixCategory,
    log: str,
) -> None:
    monkeypatch.setattr(
        "cascade.gate_fix._run_current_gate_probe",
        lambda **kwargs: (
            False,
            log,
            hook,
            category,
            True,
            "probe-signature",
            str(kwargs.get("gate_command") or ""),
        ),
    )


class TestGateFailureClassification:
    def test_classify_docstring_failure_as_fixable(self) -> None:
        log = """
D103: Missing docstring in public function
jungle/audit/messages.py:42: Missing docstring
"""
        category = classify_failure_as_model_fixable(log, "backend-docstring")
        assert category == GateFixCategory.DOCSTRING
        assert is_model_fixable(category)

    def test_classify_branch_mismatch_as_deterministic(self) -> None:
        category = classify_failure_as_model_fixable(
            "Branch mismatch: expected agent/test, found staging",
            "branch-check",
        )
        assert category == GateFixCategory.BRANCH_MISMATCH
        assert not is_model_fixable(category)

    def test_classify_workflow_metadata_as_deterministic(self) -> None:
        category = classify_failure_as_model_fixable(
            "mandate metadata\nrequired mandate metadata is missing",
            "mandate-check",
        )
        assert category == GateFixCategory.WORKFLOW
        assert not is_model_fixable(category)


class TestDeterministicEditApplication:
    def test_apply_model_fixes_structured_replace(self, tmp_path: Path) -> None:
        file_path = tmp_path / "pkg" / "module.py"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")

        response = """```json
{
  "summary": "Fix return value",
  "edits": [
    {
      "path": "pkg/module.py",
      "old_text": "return 1",
      "new_text": "return 2"
    }
  ]
}
```"""

        success, changed_files, message = apply_model_fixes(tmp_path, response)

        assert success is True
        assert changed_files == ["pkg/module.py"]
        assert "deterministic" in message.lower()
        assert "return 2" in file_path.read_text(encoding="utf-8")

    def test_apply_model_fixes_full_file_content(self, tmp_path: Path) -> None:
        response = "```json\n" + json.dumps(
            {
                "summary": "Create helper",
                "edits": [
                    {
                        "path": "pkg/helper.py",
                        "content": "def helper() -> str:\n    return 'ok'\n",
                    }
                ],
            }
        ) + "\n```"

        success, changed_files, _message = apply_model_fixes(tmp_path, response)

        assert success is True
        assert changed_files == ["pkg/helper.py"]
        assert (tmp_path / "pkg" / "helper.py").read_text(encoding="utf-8") == "def helper() -> str:\n    return 'ok'\n"


class TestModelStreaming:
    def test_stream_openrouter_request_surfaces_tokens_and_writes_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"x-request-id": "req-1", "content-type": "text/event-stream"}

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self, decode_unicode: bool = True):
                yield 'data: {"id": "req-1", "model": "deepseek/deepseek-v3.2", "choices": [{"delta": {"content": "hello "}}]}'
                yield 'data: {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]}'
                yield "data: [DONE]"

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr("cascade.gate_fix.requests.post", lambda *args, **kwargs: FakeResponse())

        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True)
        config = GateFixConfig(model="deepseek/deepseek-v3.2", stream=True)

        response_text, request_metadata, response_metadata = stream_openrouter_request(
            "deepseek/deepseek-v3.2",
            [{"role": "user", "content": "fix this"}],
            config,
            run_dir,
            1,
        )

        output = capsys.readouterr().out
        assert response_text == "hello world"
        assert request_metadata["stream"] is True
        assert response_metadata["status_code"] == 200
        assert response_metadata["response_id"] == "req-1"
        assert "[model] hello world" in output
        assert output.count("[model]") == 1
        assert (run_dir / "gate_fix_attempt_1.stream.log").exists()

    def test_stream_openrouter_request_assembles_fragmented_tokens_without_prefix_spam(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"x-request-id": "req-2", "content-type": "text/event-stream"}

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self, decode_unicode: bool = True):
                yield 'data: {"choices": [{"delta": {"content": "Ana"}}]}'
                yield 'data: {"choices": [{"delta": {"content": "lyz"}}]}'
                yield 'data: {"choices": [{"delta": {"content": "ing "}}]}'
                yield 'data: {"choices": [{"delta": {"content": "now"}, "finish_reason": "stop"}]}'
                yield "data: [DONE]"

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr("cascade.gate_fix.requests.post", lambda *args, **kwargs: FakeResponse())

        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True)
        config = GateFixConfig(model="deepseek/deepseek-v3.2", stream=True)

        response_text, _request_metadata, _response_metadata = stream_openrouter_request(
            "deepseek/deepseek-v3.2",
            [{"role": "user", "content": "fix this"}],
            config,
            run_dir,
            2,
        )

        output = capsys.readouterr().out
        assert response_text == "Analyzing now"
        assert "[model] Analyzing now" in output
        assert output.count("[model]") == 1

    def test_stream_openrouter_request_applies_prefix_once_per_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"x-request-id": "req-3", "content-type": "text/event-stream"}

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self, decode_unicode: bool = True):
                yield 'data: {"choices": [{"delta": {"content": "Line one\\n"}}]}'
                yield 'data: {"choices": [{"delta": {"content": "Line two"}, "finish_reason": "stop"}]}'
                yield "data: [DONE]"

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr("cascade.gate_fix.requests.post", lambda *args, **kwargs: FakeResponse())

        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True)
        config = GateFixConfig(model="deepseek/deepseek-v3.2", stream=True)

        response_text, _request_metadata, _response_metadata = stream_openrouter_request(
            "deepseek/deepseek-v3.2",
            [{"role": "user", "content": "fix this"}],
            config,
            run_dir,
            3,
        )

        output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert response_text == "Line one\nLine two"
        assert output_lines == ["[model] Line one", "[model] Line two"]

    def test_stream_openrouter_request_threshold_flush_keeps_readable_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"x-request-id": "req-4", "content-type": "text/event-stream"}

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self, decode_unicode: bool = True):
                yield 'data: {"choices": [{"delta": {"content": "This stream is intentionally long so the threshold flush triggers before finish and keeps output readable for humans. "}}]}'
                yield 'data: {"choices": [{"delta": {"content": "Second sentence arrives clearly."}, "finish_reason": "stop"}]}'
                yield "data: [DONE]"

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr("cascade.gate_fix.requests.post", lambda *args, **kwargs: FakeResponse())

        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True)
        config = GateFixConfig(model="deepseek/deepseek-v3.2", stream=True)

        _response_text, _request_metadata, _response_metadata = stream_openrouter_request(
            "deepseek/deepseek-v3.2",
            [{"role": "user", "content": "fix this"}],
            config,
            run_dir,
            4,
        )

        output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(output_lines) >= 2
        assert all(line.startswith("[model] ") for line in output_lines)
        assert all("[model][model]" not in line for line in output_lines)


class TestLoopBehavior:
    def test_single_file_typing_failure_routes_file_local_batch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        target = worktree / "pkg" / "typed.py"
        target.parent.mkdir(parents=True)
        target.write_text("def f(x: int) -> int:\n    return x\n", encoding="utf-8")
        other = worktree / "pkg" / "other.py"
        other.write_text("def g() -> None:\n    pass\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured_targets: list[list[str]] = []

        def _prompt(**kwargs: Any) -> str:
            target_files = kwargs.get("target_files")
            captured_targets.append(list(target_files) if isinstance(target_files, list) else [])
            return "fix prompt"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["pkg/typed.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", lambda *args, **kwargs: (True, "ok"))

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="pyright",
            failing_log=(
                "pkg/typed.py:10: error: Incompatible types in assignment\n"
                "pkg/typed.py:14: error: Expression of type \"str\" cannot be assigned to type \"int\"\n"
                "pkg/other.py:2: note: revealed type is \"Any\""
            ),
            failing_category=GateFixCategory.TYPING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/typed.py", "pkg/other.py"]},
        )

        assert result.success is True
        assert captured_targets
        assert captured_targets[0] == ["pkg/typed.py"]

    def test_ambiguous_repeated_matches_trigger_full_file_strategy_on_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        target = worktree / "pkg" / "typed.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\nx = 1\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        patch_modes: list[str] = []
        attempt_state = {"count": 0}

        def _prompt(**kwargs: Any) -> str:
            patch_modes.append(str(kwargs.get("patch_mode_preference") or ""))
            return "fix prompt"

        def _apply(worktree: Path, model_response: str) -> tuple[bool, list[str], str]:
            attempt_state["count"] += 1
            if attempt_state["count"] == 1:
                return False, [], "Search text for pkg/typed.py matched 2 times; refusing ambiguous patch."
            return True, ["pkg/typed.py"], "Applied structured deterministic edits."

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr("cascade.gate_fix.apply_model_fixes", _apply)
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", lambda *args, **kwargs: (True, "ok"))

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="pyright",
            failing_log="pkg/typed.py:5: error: Incompatible types",
            failing_category=GateFixCategory.TYPING,
            config=GateFixConfig(model="primary", max_attempts=2, fallback_models=["fallback-1"]),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/typed.py"]},
        )

        assert result.success is True
        assert len(patch_modes) >= 2
        assert patch_modes[0] == "anchored_edits"
        assert patch_modes[1] == "full_file"

    def test_file_batch_mode_avoids_broad_multi_file_batch_when_one_file_dominates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        (worktree / "pkg").mkdir(parents=True)
        for name in ("a.py", "b.py", "c.py"):
            (worktree / "pkg" / name).write_text("x = 1\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured_targets: list[list[str]] = []

        def _prompt(**kwargs: Any) -> str:
            target_files = kwargs.get("target_files")
            captured_targets.append(list(target_files) if isinstance(target_files, list) else [])
            return "fix prompt"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["pkg/a.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", lambda *args, **kwargs: (True, "ok"))

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="ruff",
            failing_log=(
                "pkg/a.py:10: F401 unused import\n"
                "pkg/a.py:11: F841 local variable assigned but never used\n"
                "pkg/a.py:12: E302 expected 2 blank lines\n"
                "pkg/b.py:2: F401 unused import\n"
                "pkg/c.py:2: F401 unused import"
            ),
            failing_category=GateFixCategory.LINTING,
            config=GateFixConfig(model="primary", max_attempts=1, batch_mode=GateFixBatchMode.FILE),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/a.py", "pkg/b.py", "pkg/c.py"]},
        )

        assert result.success is True
        assert captured_targets
        assert captured_targets[0] == ["pkg/a.py"]

    def test_second_pass_includes_referenced_baseline_config_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale baseline entry is fixed deterministically before any model call."""
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        impl = worktree / "pkg" / "typed.py"
        impl.parent.mkdir(parents=True)
        impl.write_text("def f(): pass\n", encoding="utf-8")

        baseline = worktree / "config" / "complexity" / "c901-baseline.txt"
        baseline.parent.mkdir(parents=True)
        baseline.write_text("pkg/typed.py|f:5\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured_targets: list[list[str]] = []

        def _prompt(**kwargs: Any) -> str:
            captured_targets.append(list(kwargs.get("target_files") or []))
            return "fix prompt"

        attempt_state = {"count": 0}

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int) -> tuple[bool, str]:
            attempt_state["count"] += 1
            if attempt_state["count"] == 1:
                return False, (
                    "complexity-baseline check failed\n"
                    "pkg/typed.py:8: C901 'f' is too complex\n"
                    "stale entry in config/complexity/c901-baseline.txt: remove pkg/typed.py|f\n"
                )
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda w, r: (True, ["pkg/typed.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="pyright",
            failing_log="pkg/typed.py:5: error: Incompatible types",
            failing_category=GateFixCategory.TYPING,
            config=GateFixConfig(model="primary", max_attempts=2),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/typed.py"]},
        )

        assert result.success is True
        assert len(captured_targets) == 1
        assert baseline.read_text(encoding="utf-8") == ""

    def test_complexity_baseline_stale_entry_fixable_in_next_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real a3 stale complexity baseline line is removed deterministically without model."""
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        impl = worktree / "inventory" / "services" / "marketplace_presence.py"
        impl.parent.mkdir(parents=True)
        impl.write_text("def _sync_listing(): pass\n", encoding="utf-8")

        baseline = worktree / "config" / "complexity" / "c901-baseline.txt"
        baseline.parent.mkdir(parents=True)
        baseline.write_text("inventory/services/marketplace_presence.py|_sync_listing:15\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured_targets: list[list[str]] = []

        def _prompt(**kwargs: Any) -> str:
            captured_targets.append(list(kwargs.get("target_files") or []))
            return "fix prompt"

        attempt_state = {"count": 0}

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int) -> tuple[bool, str]:
            attempt_state["count"] += 1
            if attempt_state["count"] == 1:
                return False, (
                    "complexity gate\n"
                    "config/complexity/c901-baseline.txt: stale entry "
                    "inventory/services/marketplace_presence.py|_sync_listing — "
                    "function no longer exists or was refactored\n"
                )
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda w, r: (True, ["inventory/services/marketplace_presence.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="ruff",
            failing_log="inventory/services/marketplace_presence.py:10: W0611 unused variable",
            failing_category=GateFixCategory.LINTING,
            config=GateFixConfig(model="primary", max_attempts=2),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["inventory/services/marketplace_presence.py"]},
        )

        assert result.success is True
        assert len(captured_targets) == 1
        assert baseline.read_text(encoding="utf-8") == ""

    def test_rerun_expansion_stays_narrow(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Expansion from rerun evidence only adds explicitly referenced files, not unrelated modules."""
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        # Many unrelated implementation files that should NOT be pulled in.
        (worktree / "pkg").mkdir(parents=True)
        for i in range(8):
            (worktree / "pkg" / f"module{i}.py").write_text("x = 1\n", encoding="utf-8")

        impl = worktree / "pkg" / "impl.py"
        impl.write_text("def f(): pass\n", encoding="utf-8")

        # Only ONE baseline file explicitly referenced in rerun output.
        baseline = worktree / "config" / "c901-baseline.txt"
        baseline.parent.mkdir(parents=True)
        baseline.write_text("pkg/impl.py|f:5\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured_targets: list[list[str]] = []

        def _prompt(**kwargs: Any) -> str:
            captured_targets.append(list(kwargs.get("target_files") or []))
            return "fix prompt"

        attempt_state = {"count": 0}

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int) -> tuple[bool, str]:
            attempt_state["count"] += 1
            if attempt_state["count"] == 1:
                # Only references config/c901-baseline.txt, not any of the module0..7 files.
                return False, "stale entry in config/c901-baseline.txt: pkg/impl.py|f\n"
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda w, r: (True, ["pkg/impl.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="ruff",
            failing_log="pkg/impl.py:5: C901 too complex",
            failing_category=GateFixCategory.LINTING,
            config=GateFixConfig(model="primary", max_attempts=2),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/impl.py"]},
        )

        assert result.success is True
        assert len(captured_targets) == 2
        # Expansion is narrow: impl.py + baseline file only; the 8 module files must not appear.
        assert len(captured_targets[1]) <= 2
        assert "config/c901-baseline.txt" in captured_targets[1]
        assert all("module" not in f for f in captured_targets[1])

    def test_current_probe_overrides_stale_context_before_model_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        target = worktree / "pkg" / "fresh.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured: dict[str, str] = {}

        monkeypatch.setattr(
            "cascade.gate_fix._run_current_gate_probe",
            lambda **kwargs: (
                False,
                "pkg/fresh.py:1: F401 unused import",
                "ruff",
                GateFixCategory.LINTING,
                True,
                "fresh-signature",
                str(kwargs.get("gate_command") or ""),
            ),
        )

        def _prompt(**kwargs: Any) -> str:
            captured["hook"] = str(kwargs.get("failing_hook") or "")
            captured["log"] = str(kwargs.get("failing_log") or "")
            return "fix prompt"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["pkg/fresh.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", lambda *args, **kwargs: (True, "ok"))

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="pyright",
            failing_log="pkg/stale.py:1: error: Incompatible types",
            failing_category=GateFixCategory.TYPING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/fresh.py"]},
        )

        assert result.success is True
        assert captured["hook"] == "ruff"
        assert "F401" in captured["log"]

    def test_context_hash_change_reprobes_before_model(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        target = worktree / "pkg" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        probe_calls = {"count": 0}

        def _probe(**kwargs: Any) -> tuple[bool, str, str, GateFixCategory, bool, str, str]:
            probe_calls["count"] += 1
            return (
                False,
                "pkg/module.py:1: F401 unused import",
                "ruff",
                GateFixCategory.LINTING,
                True,
                f"sig-{probe_calls['count']}",
                str(kwargs.get("gate_command") or ""),
            )

        hash_values = iter(["hash-1", "hash-2", "hash-2"])
        monkeypatch.setattr("cascade.gate_fix._run_current_gate_probe", _probe)
        monkeypatch.setattr("cascade.gate_fix._compute_failure_context_hash", lambda **kwargs: next(hash_values))
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["pkg/module.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", lambda *args, **kwargs: (True, "ok"))

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="pyright",
            failing_log="pkg/module.py:1: error",
            failing_category=GateFixCategory.TYPING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        assert result.success is True
        assert probe_calls["count"] == 2

    def test_rerun_support_files_persist_across_fallback_attempts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        impl = worktree / "pkg" / "impl.py"
        impl.parent.mkdir(parents=True)
        impl.write_text("x = 1\n", encoding="utf-8")
        support = worktree / "config" / "c901-baseline.txt"
        support.parent.mkdir(parents=True)
        support.write_text("pkg/impl.py|f:5\n", encoding="utf-8")

        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        captured_targets: list[list[str]] = []
        patch_modes: list[str] = []
        apply_state = {"count": 0}

        def _prompt(**kwargs: Any) -> str:
            captured_targets.append(list(kwargs.get("target_files") or []))
            patch_modes.append(str(kwargs.get("patch_mode_preference") or ""))
            return "fix prompt"

        def _apply(worktree: Path, model_response: str) -> tuple[bool, list[str], str]:
            apply_state["count"] += 1
            if apply_state["count"] == 2:
                return False, [], "Search text for pkg/impl.py matched 0 times; refusing ambiguous patch."
            return True, ["pkg/impl.py"], "Applied structured deterministic edits."

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int) -> tuple[bool, str]:
            if attempt_number == 1:
                return False, "stale entry in config/c901-baseline.txt: pkg/impl.py|f"
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr("cascade.gate_fix.apply_model_fixes", _apply)
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="ruff",
            failing_log="pkg/impl.py:1: F401",
            failing_category=GateFixCategory.LINTING,
            config=GateFixConfig(model="primary", max_attempts=3, fallback_models=["fallback-1", "fallback-2"]),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/impl.py"]},
        )

        assert result.success is True
        assert len(captured_targets) == 3
        assert "config/c901-baseline.txt" in captured_targets[1]
        assert "config/c901-baseline.txt" in captured_targets[2]
        assert patch_modes[0] == "anchored_edits"
        assert patch_modes[2] == "full_file"

    def test_probe_pass_stops_without_model_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)

        monkeypatch.setattr(
            "cascade.gate_fix._run_current_gate_probe",
            lambda **kwargs: (
                True,
                "ok",
                "backend-docstring",
                GateFixCategory.DOCSTRING,
                True,
                "passed",
                str(kwargs.get("gate_command") or ""),
            ),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not be called")),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": []},
        )

        assert result.success is True
        assert result.attempts == []
        assert result.stop_reason == "Gate passed during current probe"

    def test_commit_gate_stages_model_changed_files_before_rerun(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        run_dir = tmp_path / "run"
        command_log: list[str] = []

        def _run_command(command: str, cwd: Path | None = None) -> SimpleNamespace:
            command_log.append(command)
            if command == "git rev-parse --abbrev-ref HEAD":
                return SimpleNamespace(stdout="agent/a1\n")
            if command.startswith("git ls-files --error-unmatch"):
                return SimpleNamespace(stdout="jungle/audit/messages.py\n")
            if command.startswith("git add --"):
                return SimpleNamespace(stdout="")
            return SimpleNamespace(stdout="")

        rerun_seen = {"value": False}

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int):
            rerun_seen["value"] = True
            assert any(cmd.startswith("git add --") for cmd in command_log)
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.run_command", _run_command)
        monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: False)
        monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: ["jungle/audit/messages.py"])
        monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
        monkeypatch.setattr("cascade.gate_fix._get_status_summary", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
        _stub_probe_failure(
            monkeypatch,
            hook="backend-docstring",
            category=GateFixCategory.DOCSTRING,
            log="D103 Missing docstring",
        )
        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: ("{}", {}, {}),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["jungle/audit/messages.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="git commit -m 'JNG-04232026-001 enrich-audit-log-messages implementation checkpoint'",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["jungle/audit/messages.py"]},
        )

        assert result.success is True
        assert rerun_seen["value"] is True
        assert any(cmd.startswith("git add --") for cmd in command_log)

    def test_non_commit_gate_does_not_stage_model_changed_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        run_dir = tmp_path / "run"
        command_log: list[str] = []

        def _run_command(command: str, cwd: Path | None = None) -> SimpleNamespace:
            command_log.append(command)
            if command == "git rev-parse --abbrev-ref HEAD":
                return SimpleNamespace(stdout="agent/a1\n")
            return SimpleNamespace(stdout="")

        monkeypatch.setattr("cascade.gate_fix.run_command", _run_command)
        monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: False)
        monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: ["jungle/audit/messages.py"])
        monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
        monkeypatch.setattr("cascade.gate_fix._get_status_summary", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
        _stub_probe_failure(
            monkeypatch,
            hook="backend-docstring",
            category=GateFixCategory.DOCSTRING,
            log="D103 Missing docstring",
        )
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["jungle/audit/messages.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.run_gate_recheck",
            lambda worktree, gate_command, run_dir, attempt_number: (True, "ok"),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make mandate-preflight MANDATE_SLUG=gate-fix-test",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["jungle/audit/messages.py"]},
        )

        assert result.success is True
        assert not any(cmd.startswith("git add --") for cmd in command_log)

    def test_commit_gate_staging_failure_stops_before_rerun(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        run_dir = tmp_path / "run"
        rerun_called = {"value": False}

        monkeypatch.setattr("cascade.gate_fix.run_command", lambda command, cwd=None: SimpleNamespace(stdout="agent/a1\n"))
        monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: False)
        monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: ["jungle/audit/messages.py"])
        monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
        monkeypatch.setattr("cascade.gate_fix._get_status_summary", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
        _stub_probe_failure(
            monkeypatch,
            hook="backend-docstring",
            category=GateFixCategory.DOCSTRING,
            log="D103 Missing docstring",
        )
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["jungle/audit/messages.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr(
            "cascade.gate_fix._stage_gate_fix_files_for_commit",
            lambda worktree, changed_files, dirty_or_staged_before: (False, "git add failed"),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.run_gate_recheck",
            lambda worktree, gate_command, run_dir, attempt_number: rerun_called.__setitem__("value", True) or (True, "ok"),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="git commit -m 'JNG-04232026-001 enrich-audit-log-messages implementation checkpoint'",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["jungle/audit/messages.py"]},
        )

        assert result.success is False
        assert result.stop_reason == "Failed to stage model-changed files for commit gate"
        assert rerun_called["value"] is False

    def test_commit_gate_stage_refuses_scratch_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        from cascade.gate_fix import _stage_gate_fix_files_for_commit as _stage

        ok, message = _stage(
            worktree,
            ["debug_notes.py"],
            {"debug_notes.py"},
        )
        assert ok is False
        assert "scratch/debug" in message

    def test_repeated_signature_stop_happens_after_staging_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        run_dir = tmp_path / "run"
        stage_called = {"value": False}

        monkeypatch.setattr("cascade.gate_fix.run_command", lambda command, cwd=None: SimpleNamespace(stdout="agent/a1\n"))
        monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: False)
        monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: ["jungle/audit/messages.py"])
        monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
        monkeypatch.setattr("cascade.gate_fix._get_status_summary", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", lambda *args, **kwargs: ("{}", {}, {}))
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["jungle/audit/messages.py"], "Applied structured deterministic edits."),
        )

        def _stage(worktree: Path, changed_files: list[str], dirty_or_staged_before: set[str]) -> tuple[bool, str]:
            stage_called["value"] = True
            return True, "Staging model-changed files for commit gate: jungle/audit/messages.py"

        monkeypatch.setattr("cascade.gate_fix._stage_gate_fix_files_for_commit", _stage)
        monkeypatch.setattr(
            "cascade.gate_fix.run_gate_recheck",
            lambda worktree, gate_command, run_dir, attempt_number: (False, "D103 Missing docstring"),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="git commit -m 'JNG-04232026-001 enrich-audit-log-messages implementation checkpoint'",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["jungle/audit/messages.py"]},
        )

        assert stage_called["value"] is True
        assert result.stop_reason == "Max attempts (1) exceeded"

    def test_run_gate_fix_loop_stops_on_cost_cap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)
        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not be called")),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_estimated_cost_usd=0.0001),
            model_profile=_make_profile("primary", input_cost=5000.0, output_cost=5000.0),
            run_dir=run_dir,
            gate_result={"touched_files": []},
        )

        assert result.success is False
        assert result.stop_reason == "Estimated cost would exceed cap"
        assert result.attempts == []

    def test_run_gate_fix_loop_uses_first_fallback_model(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        _stub_loop_basics(monkeypatch)
        models_called: list[str] = []

        def _stream(model: str, messages: list[dict[str, str]], config: GateFixConfig, run_dir: Path, attempt_number: int):
            models_called.append(model)
            raise ValueError("temporary failure")

        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", _stream)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(
                model="primary",
                max_attempts=2,
                fallback_models=["fallback-1", "fallback-2"],
            ),
            model_profile=_make_profile("primary"),
            run_dir=tmp_path / "run",
            gate_result={"touched_files": []},
            model_profiles_by_id={
                "primary": _make_profile("primary"),
                "fallback-1": _make_profile("fallback-1"),
                "fallback-2": _make_profile("fallback-2"),
            },
        )

        assert models_called == ["primary", "fallback-1"]
        assert result.success is False
        assert result.stop_reason == "Max attempts (2) exceeded"

    def test_run_gate_fix_loop_reruns_exact_gate_command_and_writes_artifacts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        source_file = worktree / "pkg" / "module.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
        run_dir = tmp_path / "run"
        commands_seen: list[str] = []

        def _run_command(command: str, cwd: Path | None = None) -> SimpleNamespace:
            if command == "git rev-parse --abbrev-ref HEAD":
                return SimpleNamespace(stdout="agent/a1\n")
            commands_seen.append(command)
            return SimpleNamespace(stdout="gate passed\n")

        monkeypatch.setattr("cascade.gate_fix.run_command", _run_command)
        monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: False)
        monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
        monkeypatch.setattr("cascade.gate_fix._get_status_summary", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
        _stub_probe_failure(
            monkeypatch,
            hook="backend-docstring",
            category=GateFixCategory.DOCSTRING,
            log="pkg/module.py:1: D103 Missing docstring",
        )
        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: (
                """```json
{
  \"summary\": \"Fix return value\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 1\",
      \"new_text\": \"return 2\"
    }
  ]
}
```""",
                {"request": "meta"},
                {"response": "meta"},
            ),
        )

        gate_command = "python -m pytest tests/test_example.py -q"
        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command=gate_command,
            failing_hook="backend-docstring",
            failing_log="pkg/module.py:1: D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary"),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        assert result.success is True
        assert source_file.read_text(encoding="utf-8") == "def value() -> int:\n    return 2\n"
        assert gate_command in commands_seen
        assert result.attempts[0].rerun_command == gate_command
        assert (run_dir / "gate_fix_attempt_1.prompt.md").exists()
        assert (run_dir / "gate_fix_model_call.json").exists()
        assert (run_dir / "gate_fix_attempt_1.rerun.log").exists()

    def test_run_gate_fix_loop_stops_on_repeated_failure_signature(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        source_file = worktree / "pkg" / "module.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")

        _stub_loop_basics(monkeypatch)
        failing_log = "pkg/module.py:1: D103 Missing docstring"
        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: (
                """```json
{
  \"summary\": \"Fix return value\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 1\",
      \"new_text\": \"return 2\"
    }
  ]
}
```""",
                {},
                {},
            ),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["pkg/module.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.run_gate_recheck",
            lambda worktree, gate_command, run_dir, attempt_number: (False, failing_log),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="backend-docstring",
            failing_log=failing_log,
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=1),
            model_profile=_make_profile("primary"),
            run_dir=tmp_path / "run",
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        assert result.success is False
        assert result.stop_reason == "Max attempts (1) exceeded"

    def test_run_gate_fix_loop_reclassifies_new_failure_and_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        source_file = worktree / "pkg" / "module.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)

        seen_hooks: list[str] = []

        def _prompt(**kwargs: Any) -> str:
            seen_hooks.append(str(kwargs["failing_hook"]))
            return "fix prompt"

        attempt_state = {"count": 0}

        def _stream(*args: Any, **kwargs: Any):
            attempt_state["count"] += 1
            if attempt_state["count"] == 1:
                return (
                    """```json
{
  \"summary\": \"first fix\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 1\",
      \"new_text\": \"return 2\"
    }
  ]
}
```""",
                    {},
                    {},
                )
            return (
                """```json
{
  \"summary\": \"second fix\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 2\",
      \"new_text\": \"return 3\"
    }
  ]
}
```""",
                {},
                {},
            )

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int):
            if attempt_number == 1:
                return False, "Failed: pyright\nerror: Incompatible types"
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.build_gate_fix_prompt", _prompt)
        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", _stream)
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="git commit -m 'JNG-04232026-001 update docs'",
            failing_hook="backend-docstring",
            failing_log="pkg/module.py:1: D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=3),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        output = capsys.readouterr().out
        assert result.success is True
        assert seen_hooks[0] == "backend-docstring"
        assert seen_hooks[1] == "pyright"
        assert "[rerun] hook/check: pyright" in output
        assert "[rerun] category: typing" in output
        assert "[rerun] model-fixable: yes" in output
        assert "[rerun] continue: yes" in output
        assert (run_dir / "gate_fix_attempt_1.failure_context.json").exists()
        assert (run_dir / "gate_fix_latest_failure_context.json").exists()

    def test_run_gate_fix_loop_stops_on_repeated_signature_after_fix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        source_file = worktree / "pkg" / "module.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
        _stub_loop_basics(monkeypatch)

        failing_log = "pkg/module.py:1: D103 Missing docstring"
        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: (
                """```json
{
  \"summary\": \"Fix return value\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 1\",
      \"new_text\": \"return 2\"
    }
  ]
}
```""",
                {},
                {},
            ),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.apply_model_fixes",
            lambda worktree, model_response: (True, ["pkg/module.py"], "Applied structured deterministic edits."),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.run_gate_recheck",
            lambda worktree, gate_command, run_dir, attempt_number: (False, failing_log),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="git commit -m 'JNG-04232026-001 update docs'",
            failing_hook="backend-docstring",
            failing_log=failing_log,
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=3),
            model_profile=_make_profile("primary"),
            run_dir=tmp_path / "run",
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        assert result.success is False
        assert result.stop_reason == "Repeated same failure signature"
        assert len(result.attempts) == 2

    def test_run_gate_fix_loop_stops_on_deterministic_new_failure_with_guidance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        source_file = worktree / "pkg" / "module.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)

        monkeypatch.setattr(
            "cascade.gate_fix.stream_openrouter_request",
            lambda *args, **kwargs: (
                """```json
{
  \"summary\": \"Fix return value\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 1\",
      \"new_text\": \"return 2\"
    }
  ]
}
```""",
                {},
                {},
            ),
        )
        monkeypatch.setattr(
            "cascade.gate_fix.run_gate_recheck",
            lambda worktree, gate_command, run_dir, attempt_number: (
                False,
                "Branch mismatch: expected agent/a1/gate-fix-test, found staging",
            ),
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="git commit -m 'JNG-04232026-001 update docs'",
            failing_hook="backend-docstring",
            failing_log="pkg/module.py:1: D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=3),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        output = capsys.readouterr().out
        assert result.success is False
        assert result.stop_reason == "Deterministic non-model failure after rerun"
        assert result.attempts[0].failure_reason
        assert "[rerun] model-fixable: no" in output
        assert "[stop] deterministic repair suggested" in output

    def test_run_gate_fix_loop_stops_on_branch_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        monkeypatch.setattr(
            "cascade.gate_fix.run_command",
            lambda command, cwd=None: SimpleNamespace(stdout="agent/a1\n"),
        )
        monkeypatch.setattr("cascade.gate_fix.check_branch_drift", lambda worktree, expected_branch: True)
        monkeypatch.setattr("cascade.gate_fix.get_current_dirty_files", lambda worktree: [])
        monkeypatch.setattr("cascade.gate_fix._get_diff_size", lambda worktree: 0)
        monkeypatch.setattr("cascade.gate_fix.read_mandate_metadata", lambda worktree, mandate_slug: None)
        _stub_probe_failure(
            monkeypatch,
            hook="backend-docstring",
            category=GateFixCategory.DOCSTRING,
            log="D103 Missing docstring",
        )

        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command="make preflight",
            failing_hook="backend-docstring",
            failing_log="D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary"),
            model_profile=_make_profile("primary"),
            run_dir=tmp_path / "run",
            gate_result={"touched_files": []},
        )

        assert result.success is False
        assert result.stop_reason == "Branch drift detected before model attempt"

    def test_run_gate_fix_loop_retries_exact_commit_command_until_pass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        source_file = worktree / "pkg" / "module.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
        run_dir = tmp_path / "run"
        _stub_loop_basics(monkeypatch)

        attempt_state = {"count": 0}
        gate_calls: list[str] = []

        def _stream(*args: Any, **kwargs: Any):
            attempt_state["count"] += 1
            if attempt_state["count"] == 1:
                return (
                    """```json
{
  \"summary\": \"first fix\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 1\",
      \"new_text\": \"return 2\"
    }
  ]
}
```""",
                    {},
                    {},
                )
            return (
                """```json
{
  \"summary\": \"second fix\",
  \"edits\": [
    {
      \"path\": \"pkg/module.py\",
      \"old_text\": \"return 2\",
      \"new_text\": \"return 3\"
    }
  ]
}
```""",
                {},
                {},
            )

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int):
            gate_calls.append(gate_command)
            if attempt_number == 1:
                return False, "Failed: pyright\nerror: Incompatible types"
            return True, "ok"

        monkeypatch.setattr("cascade.gate_fix.stream_openrouter_request", _stream)
        monkeypatch.setattr("cascade.gate_fix.run_gate_recheck", _recheck)

        commit_command = "git commit -m 'JNG-04232026-001 update docs'"
        result = run_gate_fix_loop(
            worktree=worktree,
            project_name="jungle",
            agent="a1",
            mandate_slug="gate-fix-test",
            gate_command=commit_command,
            failing_hook="backend-docstring",
            failing_log="pkg/module.py:1: D103 Missing docstring",
            failing_category=GateFixCategory.DOCSTRING,
            config=GateFixConfig(model="primary", max_attempts=3),
            model_profile=_make_profile("primary"),
            run_dir=run_dir,
            gate_result={"touched_files": ["pkg/module.py"]},
        )

        assert result.success is True
        assert gate_calls == [commit_command, commit_command]
        assert source_file.read_text(encoding="utf-8") == "def value() -> int:\n    return 3\n"


class TestCliIntegration:
    def test_gate_fix_cli_rejects_deterministic_failures(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _setup_gate_fix_agent(
            tmp_path,
            log_content="Branch mismatch: expected agent/test, found staging\n",
            hook="branch-check",
        )

        called = {"value": False}
        monkeypatch.setattr(
            cli_module,
            "run_gate_fix_loop",
            lambda **kwargs: called.__setitem__("value", True),
        )

        runner = CliRunner()
        result = runner.invoke(app, ["gate-fix", "a1", "--project", "jungle"])

        assert result.exit_code == 1
        assert called["value"] is False
        assert "not model-fixable" in result.output

    def test_gate_fix_cli_uses_default_cheap_fixer_and_writes_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _project_file, _worktree, run_dir = _setup_gate_fix_agent(
            tmp_path,
            log_content="pkg/module.py:1: D103 Missing docstring\n",
            hook="backend-docstring",
            touched_files=["pkg/module.py"],
        )

        captured: dict[str, Any] = {}

        def _run_loop(**kwargs: Any) -> GateFixResult:
            captured.update(kwargs)
            return GateFixResult(
                success=True,
                attempts=[],
                total_estimated_cost=0.01,
                stop_reason="Gate passed",
                initial_model=kwargs["config"].model,
                fallback_chain=list(kwargs["config"].fallback_models or []),
            )

        monkeypatch.setattr(cli_module, "run_gate_fix_loop", _run_loop)

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "gate-fix",
                "a1",
                "--project",
                "jungle",
                "--max-estimated-cost",
                "0.50",
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["run_dir"] == run_dir
        assert captured["config"].model == "deepseek/deepseek-v3.2"
        assert captured["config"].max_estimated_cost_usd == pytest.approx(0.50)
        assert captured["model_profile"].model == "deepseek/deepseek-v3.2"
        assert captured["model_profiles_by_id"]["qwen/qwen3-coder-480b-a35b-instruct:free"].model == "qwen/qwen3-coder-480b-a35b-instruct:free"
        assert (run_dir / "gate_fix_summary.json").exists()
        assert "Profile: cheap-fixer" in result.output

    def test_gate_fix_cli_prefers_commit_failure_source_over_stale_workflow_gate_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _project_file, _worktree, run_dir = _setup_gate_fix_agent(
            tmp_path,
            log_content="missing mandate metadata\nworkflow gate failed\n",
            hook="mandate-metadata",
        )

        commit_context = {
            "source": "closeout-prep-commit",
            "timestamp": "2026-04-23T00:00:00Z",
            "command": "git commit -m 'JNG-04232026-001 checkpoint'",
            "hook": "backend-docstring",
            "log": "pkg/module.py:1: D103 Missing docstring",
            "log_path": str(run_dir / "closeout_prep_commit_failure.log"),
            "touched_files": ["pkg/module.py"],
        }
        (run_dir / "closeout_prep_commit_failure.json").write_text(
            json.dumps(commit_context, indent=2) + "\n",
            encoding="utf-8",
        )

        captured: dict[str, Any] = {}

        def _run_loop(**kwargs: Any) -> GateFixResult:
            captured.update(kwargs)
            return GateFixResult(
                success=True,
                attempts=[],
                total_estimated_cost=0.01,
                stop_reason="Gate passed",
                initial_model=kwargs["config"].model,
            )

        monkeypatch.setattr(cli_module, "run_gate_fix_loop", _run_loop)

        runner = CliRunner()
        result = runner.invoke(app, ["gate-fix", "a1", "--project", "jungle"])

        assert result.exit_code == 0, result.output
        assert captured["failing_hook"] == "backend-docstring"
        assert captured["gate_command"] == "git commit -m 'JNG-04232026-001 checkpoint'"
        assert "Source: closeout-prep-commit" in result.output

    def test_gate_fix_resolve_source_prefers_explicit_context_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _project_file, _worktree, run_dir = _setup_gate_fix_agent(
            tmp_path,
            log_content="missing mandate metadata\nworkflow gate failed\n",
            hook="mandate-metadata",
        )

        (run_dir / "closeout_prep_commit_failure.json").write_text(
            json.dumps(
                {
                    "source": "closeout-prep-commit",
                    "command": "git commit -m 'old'",
                    "hook": "backend-docstring",
                    "log": "D103 Missing docstring",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        explicit_path = run_dir / "explicit_failure.json"
        explicit_path.write_text(
            json.dumps(
                {
                    "command": "make explicit-failing-gate",
                    "hook": "backend-docstring",
                    "log": "explicit failure",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        resolved = cli_module._resolve_gate_fix_failure_source(
            run_dir=run_dir,
            explicit_context_file=explicit_path,
        )

        assert resolved is not None
        assert resolved["source"] == "explicit-context-file"
        assert resolved["command"] == "make explicit-failing-gate"

    def test_gate_fix_cli_a3_pattern_typing_then_deterministic_baseline_then_pass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _project_file, worktree, run_dir = _setup_gate_fix_agent(
            tmp_path,
            log_content="stale typing context that should be replaced by probe\n",
            hook="pyright",
            touched_files=["inventory/services/marketplace_presence.py"],
        )

        impl = worktree / "inventory" / "services" / "marketplace_presence.py"
        impl.parent.mkdir(parents=True, exist_ok=True)
        impl.write_text("def _sync_listing() -> int:\n    return 1\n", encoding="utf-8")
        baseline = worktree / "config" / "complexity" / "c901-baseline.txt"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text("inventory/services/marketplace_presence.py|_sync_listing:15\n", encoding="utf-8")

        commit_context = {
            "source": "closeout-prep-commit",
            "timestamp": "2026-04-25T00:00:00Z",
            "command": "git commit -m 'JNG-04252026-001 a3 checkpoint'",
            "hook": "pyright",
            "log": "inventory/services/marketplace_presence.py:12: error: Incompatible return value type",
            "log_path": str(run_dir / "closeout_prep_commit_failure.log"),
            "touched_files": ["inventory/services/marketplace_presence.py"],
        }
        (run_dir / "closeout_prep_commit_failure.json").write_text(
            json.dumps(commit_context, indent=2) + "\n",
            encoding="utf-8",
        )

        command_log: list[str] = []

        def _run_command(command: str, cwd: Path | None = None) -> SimpleNamespace:
            command_log.append(command)
            if command == "git rev-parse --abbrev-ref HEAD":
                return SimpleNamespace(stdout="agent/a1\n")
            if command == "git rev-parse HEAD":
                return SimpleNamespace(stdout="abc123\n")
            if command.startswith("git diff"):
                return SimpleNamespace(stdout="")
            if command.startswith("git ls-files --error-unmatch"):
                return SimpleNamespace(stdout="inventory/services/marketplace_presence.py\n")
            if command.startswith("git add --"):
                return SimpleNamespace(stdout="")
            return SimpleNamespace(stdout="")

        recheck_state = {"count": 0}
        probe_commands: list[str] = []

        def _recheck(worktree: Path, gate_command: str, run_dir: Path, attempt_number: int) -> tuple[bool, str]:
            recheck_state["count"] += 1
            if attempt_number == 0:
                probe_commands.append(gate_command)
                return False, "inventory/services/marketplace_presence.py:12: error: Incompatible return value type"
            if attempt_number == 1:
                return False, (
                    "complexity gate\n"
                    "config/complexity/c901-baseline.txt: stale entry "
                    "inventory/services/marketplace_presence.py|_sync_listing\n"
                )
            return True, "ok"

        stream_calls = {"count": 0}

        def _stream(model: str, messages: list[dict[str, str]], config: GateFixConfig, run_dir: Path, attempt_number: int):
            stream_calls["count"] += 1
            response = "```json\n" + json.dumps(
                {
                    "summary": "Fix typing in marketplace presence",
                    "edits": [
                        {
                            "path": "inventory/services/marketplace_presence.py",
                            "old_text": "return 1",
                            "new_text": "return 2",
                        }
                    ],
                }
            ) + "\n```"
            return response, {"model": model}, {"status_code": 200}

        monkeypatch.setattr(gate_fix_module, "run_command", _run_command)
        monkeypatch.setattr(gate_fix_module, "run_gate_recheck", _recheck)
        monkeypatch.setattr(gate_fix_module, "stream_openrouter_request", _stream)
        monkeypatch.setattr(gate_fix_module, "check_branch_drift", lambda worktree, expected_branch: False)
        monkeypatch.setattr(gate_fix_module, "read_mandate_metadata", lambda worktree, mandate_slug: None)

        runner = CliRunner()
        result = runner.invoke(app, ["gate-fix", "a1", "--project", "jungle", "--max-attempts", "3"])

        assert result.exit_code == 0, result.output
        assert stream_calls["count"] == 1
        assert baseline.read_text(encoding="utf-8") == ""
        assert "[probe] running current gate probe" in result.output
        assert "[deterministic] removed stale complexity baseline entry" in result.output
        assert probe_commands
        assert probe_commands[0].startswith("git commit -m")


class TestGateFixConfigHelpers:
    def test_resolve_gate_fix_model_profile_prefers_project_override(self, tmp_path: Path) -> None:
        project_file = _write_project_file(
            tmp_path,
            extra_profiles="""    cheap-fixer:
      provider: openrouter
      model: custom/custom-fixer
      input_cost_per_million: 1.0
      output_cost_per_million: 2.0
""",
        )

        project_config = load_project_config(project_file)
        profile = resolve_gate_fix_model_profile(project_config, "cheap-fixer")

        assert profile.model == "custom/custom-fixer"


class TestSerializationHelpers:
    def test_attempt_and_result_serialization(self, tmp_path: Path) -> None:
        attempt = GateFixAttempt(
            attempt_number=1,
            model="deepseek/deepseek-v3.2",
            prompt_tokens=1000,
            expected_output_tokens=8000,
            estimated_cost=0.05,
            request_metadata={"trace": "value"},
            response_summary="fixed",
            changed_files=["pkg/module.py"],
            success=True,
        )
        result = GateFixResult(
            success=True,
            attempts=[attempt],
            total_estimated_cost=0.05,
            stop_reason="Gate passed",
            initial_model="deepseek/deepseek-v3.2",
        )

        data = result.to_dict()
        assert data["attempts_count"] == 1
        assert data["total_estimated_cost"] == "$0.0500"

        summary_path = save_gate_fix_summary(tmp_path, result)
        assert summary_path.exists()
        content = json.loads(summary_path.read_text(encoding="utf-8"))
        assert content["success"] is True


class TestGuardHelpers:
    def test_detect_unrelated_file_growth(self) -> None:
        assert not detect_unrelated_file_growth({"file1.py"}, {"file1.py", "file2.py"}, ["file2.py"])
        assert detect_unrelated_file_growth({"file1.py"}, {"file1.py", "a.py", "b.py", "c.py", "d.py", "e.py", "f.py"}, ["file1.py"])