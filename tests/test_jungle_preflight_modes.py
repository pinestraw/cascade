from __future__ import annotations

from pathlib import Path

from cascade.config import load_project_config


def test_default_jungle_example_uses_full_mandate_preflight() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    project_file = repo_root / "examples" / "jungle.yaml"
    project = load_project_config(project_file)

    preflight_cmd = project.commands.preflight or ""
    assert preflight_cmd == "make mandate-preflight MANDATE_SLUG={slug}"
    assert "MANDATE_PREFLIGHT_BACKEND_TEST_CMD=true" not in preflight_cmd
    assert "MANDATE_PREFLIGHT_FRONTEND_TEST_CMD=true" not in preflight_cmd
    assert "MANDATE_PREFLIGHT_MUTATION_CMD=true" not in preflight_cmd


def test_smoke_script_fast_mode_explicitly_disables_heavy_phases() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    smoke_script = repo_root / "tests" / "smoke" / "smoke_host_native_metadata_mandate.sh"
    content = smoke_script.read_text(encoding="utf-8")

    assert 'FAST_PREFLIGHT="${FAST_PREFLIGHT:-0}"' in content
    assert "if [[ \"$FAST_PREFLIGHT\" == \"1\" ]]; then" in content
    assert "MANDATE_PREFLIGHT_BACKEND_TEST_CMD=true" in content
    assert "MANDATE_PREFLIGHT_FRONTEND_TEST_CMD=true" in content
    assert "MANDATE_PREFLIGHT_MUTATION_CMD=true" in content


def test_default_mode_preserves_scope_aware_jungle_preflight_behavior() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    project_file = repo_root / "examples" / "jungle.yaml"
    project = load_project_config(project_file)

    # Default command delegates to Jungle's mandate-preflight script, which
    # already implements scope-aware backend/frontend/mutation phase skipping.
    preflight_cmd = project.commands.preflight or ""
    assert preflight_cmd == "make mandate-preflight MANDATE_SLUG={slug}"
    assert "MANDATE_PREFLIGHT_" not in preflight_cmd


def test_smoke_script_documents_and_enforces_idempotent_cleanup() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    smoke_script = repo_root / "tests" / "smoke" / "smoke_host_native_metadata_mandate.sh"
    content = smoke_script.read_text(encoding="utf-8")

    assert "Idempotency:" in content
    assert "cleanup_previous_smoke_state" in content
    assert "git -C \"$JUNGLE_REPO_ROOT\" worktree remove --force" in content
    assert "git -C \"$JUNGLE_REPO_ROOT\" worktree prune" in content
    assert "rm -f \"$AGENT_STATE_FILE\"" in content
    assert "rm -rf \"$AGENT_RUN_DIR\"" in content
