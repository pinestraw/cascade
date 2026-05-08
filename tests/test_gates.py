"""Tests for cascade.gates — deterministic gate storage and staleness tracking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cascade.gates import (
    GATE_RESULT_FILENAME,
    _extract_failed_hooks,
    build_failure_summary,
    check_gate_staleness,
    classify_gate_failure,
    failure_signature,
    gate_status_line,
    get_diff_fingerprint,
    get_git_head_sha,
    get_touched_files,
    load_gate_result,
    save_gate_result,
)


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_gate_result(tmp_path: Path) -> None:
    gate_data: dict[str, object] = {
        "timestamp": "2026-04-21T10:00:00Z",
        "command": "make preflight",
        "exit_code": 0,
        "passed": True,
        "log_path": str(tmp_path / "preflight.log"),
        "git_head_sha": "abc123",
        "diff_fingerprint": "deadbeef",
        "touched_files": ["foo.py", "bar.py"],
        "failure_summary": None,
    }
    result_path = save_gate_result(tmp_path, gate_data)
    assert result_path == tmp_path / GATE_RESULT_FILENAME
    assert result_path.exists()

    loaded = load_gate_result(tmp_path)
    assert loaded is not None
    assert loaded["passed"] is True
    assert loaded["exit_code"] == 0
    assert loaded["touched_files"] == ["foo.py", "bar.py"]


def test_load_gate_result_missing_returns_none(tmp_path: Path) -> None:
    assert load_gate_result(tmp_path) is None


def test_load_gate_result_corrupt_returns_none(tmp_path: Path) -> None:
    (tmp_path / GATE_RESULT_FILENAME).write_text("not json", encoding="utf-8")
    assert load_gate_result(tmp_path) is None


# ---------------------------------------------------------------------------
# staleness checks — no real git needed; we monkeypatch the helpers
# ---------------------------------------------------------------------------


def _make_passed_result(head: str = "aaa", fp: str = "bbb") -> dict[str, object]:
    return {
        "passed": True,
        "git_head_sha": head,
        "diff_fingerprint": fp,
    }


def test_passed_gate_not_stale_when_head_and_diff_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.gates as gates_module

    monkeypatch.setattr(gates_module, "get_git_head_sha", lambda wt: "aaa")
    monkeypatch.setattr(gates_module, "get_diff_fingerprint", lambda wt: "bbb")

    is_stale, reason = check_gate_staleness(_make_passed_result(), tmp_path)
    assert not is_stale
    assert reason == ""


def test_passed_gate_stale_when_head_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.gates as gates_module

    monkeypatch.setattr(gates_module, "get_git_head_sha", lambda wt: "zzz")
    monkeypatch.setattr(gates_module, "get_diff_fingerprint", lambda wt: "bbb")

    is_stale, reason = check_gate_staleness(_make_passed_result(head="aaa"), tmp_path)
    assert is_stale
    assert "HEAD changed" in reason


def test_passed_gate_stale_when_diff_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.gates as gates_module

    monkeypatch.setattr(gates_module, "get_git_head_sha", lambda wt: "aaa")
    monkeypatch.setattr(gates_module, "get_diff_fingerprint", lambda wt: "fff")

    is_stale, reason = check_gate_staleness(_make_passed_result(fp="bbb"), tmp_path)
    assert is_stale
    assert "diff changed" in reason


def test_failed_gate_never_reported_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.gates as gates_module

    monkeypatch.setattr(gates_module, "get_git_head_sha", lambda wt: "zzz")
    monkeypatch.setattr(gates_module, "get_diff_fingerprint", lambda wt: "fff")

    failed_result: dict[str, object] = {
        "passed": False,
        "git_head_sha": "aaa",
        "diff_fingerprint": "bbb",
    }
    is_stale, reason = check_gate_staleness(failed_result, tmp_path)
    assert not is_stale
    assert reason == ""


# ---------------------------------------------------------------------------
# failure summary (deterministic)
# ---------------------------------------------------------------------------


def test_build_failure_summary_includes_command_and_exit_code() -> None:
    gate_result: dict[str, object] = {
        "command": "make preflight",
        "exit_code": 1,
        "log_path": "/run/preflight.log",
        "touched_files": ["src/foo.py"],
    }
    summary = build_failure_summary(gate_result, "")
    assert "make preflight" in summary
    assert "exit_code" in summary.lower() or "Exit code" in summary
    assert "/run/preflight.log" in summary
    assert "src/foo.py" in summary


def test_extract_failed_hooks_precommit_format() -> None:
    log = (
        "ruff-format..........................................................Failed\n"
        "- hook id: ruff-format (exit code 1)\n"
        "Failed: pyright\n"
        "FAILED: bandit\n"
    )
    hooks = _extract_failed_hooks(log)
    assert "ruff-format" in hooks
    assert "pyright" in hooks
    assert "bandit" in hooks


def test_extract_failed_hooks_empty_log() -> None:
    assert _extract_failed_hooks("") == []


# ---------------------------------------------------------------------------
# gate_status_line
# ---------------------------------------------------------------------------


def test_gate_status_line_no_result() -> None:
    line = gate_status_line(None, None)
    assert "no result" in line


def test_gate_status_line_failed() -> None:
    result: dict[str, object] = {
        "passed": False,
        "exit_code": 2,
        "timestamp": "2026-04-21T10:00:00Z",
    }
    line = gate_status_line(result, None)
    assert "FAILED" in line
    assert "exit 2" in line


def test_gate_status_line_passed_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.gates as gates_module

    monkeypatch.setattr(gates_module, "get_git_head_sha", lambda wt: "aaa")
    monkeypatch.setattr(gates_module, "get_diff_fingerprint", lambda wt: "bbb")

    result: dict[str, object] = {
        "passed": True,
        "exit_code": 0,
        "timestamp": "2026-04-21T10:00:00Z",
        "git_head_sha": "aaa",
        "diff_fingerprint": "bbb",
    }
    line = gate_status_line(result, tmp_path)
    assert "passed" in line
    assert "STALE" not in line


def test_gate_status_line_passed_but_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.gates as gates_module

    monkeypatch.setattr(gates_module, "get_git_head_sha", lambda wt: "zzz")
    monkeypatch.setattr(gates_module, "get_diff_fingerprint", lambda wt: "bbb")

    result: dict[str, object] = {
        "passed": True,
        "exit_code": 0,
        "timestamp": "2026-04-21T10:00:00Z",
        "git_head_sha": "aaa",
        "diff_fingerprint": "bbb",
    }
    line = gate_status_line(result, tmp_path)
    assert "STALE" in line


def test_failure_signature_prefers_hook_category_and_line() -> None:
    gate_result: dict[str, object] = {
        "touched_files": ["a.py", "b.py"],
    }
    log_tail = "Failed: mypy\nerror: name 'foo' is not defined\n"
    signature = failure_signature(gate_result, log_tail)
    assert "typing" in signature
    assert "mypy" in signature
    assert "a.py" in signature
    assert "error:" in signature


def test_failure_signature_falls_back_to_hash_when_log_has_no_meaningful_lines() -> None:
    signature = failure_signature(None, "\n\n")
    assert signature
    assert "unknown" in signature


def test_classify_dirty_closeout_failure() -> None:
    log_tail = "ERROR: Unexpected dirty file while closing mandate: api/serializers/inventory.py\n"
    result = classify_gate_failure(log_tail)
    assert result["detected"] is True
    assert result["hook"] == "mandate-dirty-file"
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False
    assert result["dirty_file_path"] == "api/serializers/inventory.py"
    assert "closeout-prep" in str(result["suggested_no_model_action"])


def test_dirty_closeout_signature_includes_path_line() -> None:
    log_tail = "ERROR: Unexpected dirty file while closing mandate: api/serializers/inventory.py\n"
    signature = failure_signature(None, log_tail)
    assert "mandate-dirty-file" in signature
    assert "workflow" in signature
    assert "api/serializers/inventory.py" in signature


def test_mandate_metadata_dirty_not_classified_as_coverage() -> None:
    log_tail = (
        "M .github/mandates/audit.log\n"
        "?? .github/mandates/enrich-audit-log-messages.json\n"
        "coverage summary emitted earlier\n"
    )
    result = classify_gate_failure(log_tail)
    assert result["hook"] == "mandate-metadata"
    assert result["category"] == "workflow"
    assert result["model_recommended"] is False


def test_coverage_classification_requires_explicit_failure_text() -> None:
    non_failure_log = "coverage report generated\ncoverage: 87%\n"
    non_failure = classify_gate_failure(non_failure_log)
    assert non_failure["category"] != "coverage"

    failure_log = "FAILED: coverage threshold not met (required 90%)\n"
    failure = classify_gate_failure(failure_log)
    assert failure["category"] == "coverage"
    assert failure["hook"] == "coverage-policy"


def test_missing_workspace_link_classified_as_environment() -> None:
    log_tail = (
        "ERROR: env file /workspace/jungle-worktrees/jungle-secrets/instica/.env.local not found\n"
    )
    result = classify_gate_failure(log_tail)
    assert result["detected"] is True
    assert result["hook"] == "missing-workspace-link"
    assert result["category"] == "environment"
    assert result["strategy"] == "deterministic_repair"
    assert result["repair_kind"] == "missing-workspace-link"
    assert result["model_recommended"] is False


def test_missing_workspace_link_precedence_over_coverage_gate_name() -> None:
    log_tail = (
        "Preflight: backend changed-line coverage\n"
        "ERROR: env file /workspace/jungle-worktrees/jungle-secrets/instica/.env.local not found\n"
    )
    result = classify_gate_failure(log_tail)
    assert result["hook"] == "missing-workspace-link"
    assert result["category"] == "environment"
    assert result["model_recommended"] is False


def test_docker_buildkit_mount_error_classified_as_environment() -> None:
    log_tail = "the --mount option requires BuildKit. Refer to https://docs.docker.com/build/buildkit/ to enable it.\n"
    result = classify_gate_failure(log_tail)
    assert result["detected"] is True
    assert result["hook"] == "docker-buildkit"
    assert result["category"] == "environment"
    assert result["strategy"] == "deterministic_repair"
    assert result["repair_kind"] == "docker-buildkit"
    assert result["model_recommended"] is False
    assert "DOCKER_BUILDKIT" in str(result["suggested_no_model_action"])


def test_docker_buildkit_compose_error_classified_as_environment() -> None:
    log_tail = "Docker Compose requires buildx plugin to be installed\n"
    result = classify_gate_failure(log_tail)
    assert result["detected"] is True
    assert result["hook"] == "docker-buildkit"
    assert result["category"] == "environment"
    assert result["model_recommended"] is False


def test_docker_buildkit_classified_before_coverage() -> None:
    log_tail = (
        "Preflight: backend changed-line coverage\n"
        "Docker Compose requires buildx plugin to be installed\n"
    )
    result = classify_gate_failure(log_tail)
    assert result["hook"] == "docker-buildkit"
    assert result["category"] == "environment"
    assert result["model_recommended"] is False


def test_docker_runtime_network_error_classified_as_environment() -> None:
    log_tail = (
        "Error response from daemon: container deadbeef is not connected to the network "
        "jungle-sample_default\n"
    )
    result = classify_gate_failure(log_tail)
    assert result["detected"] is True
    assert result["hook"] == "docker-runtime-network"
    assert result["category"] == "environment"
    assert result["strategy"] == "deterministic_retry"
    assert result["repair_kind"] == "docker-runtime-network"
    assert result["model_recommended"] is False


def test_docker_runtime_network_precedence_over_coverage_text() -> None:
    log_tail = (
        "Preflight: backend changed-line coverage\n"
        "Error response from daemon: error while removing network: network "
        "jungle-sample_default has active endpoints\n"
    )
    result = classify_gate_failure(log_tail)
    assert result["hook"] == "docker-runtime-network"
    assert result["category"] == "environment"
    assert result["model_recommended"] is False
