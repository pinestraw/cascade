"""Shared pytest fixtures for the Cascade test suite.

All fixtures are deterministic and require no real OpenCode, OpenRouter, GitHub,
or jungle repo access.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Generator

import pytest

from cascade.config import (
    BranchesConfig,
    CommandsConfig,
    ContextBudget,
    ContextBudgetsConfig,
    GithubConfig,
    ModelProfile,
    ModelsConfig,
    PathsConfig,
    ProjectConfig,
    RetryPolicyConfig,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_profile(
    name: str = "test-profile",
    provider: str = "openrouter",
    model: str = "z-ai/glm-4.7-flash",
    input_cost: float = 0.06,
    output_cost: float = 0.40,
    use_for: list[str] | None = None,
) -> ModelProfile:
    return ModelProfile(
        provider=provider,
        model=model,
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
        use_for=use_for or [],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_worktree(tmp_path: Path) -> Path:
    """A real temporary directory that can serve as a worktree."""
    wt = tmp_path / "worktrees" / "oc1-test-feature"
    wt.mkdir(parents=True)
    return wt


@pytest.fixture
def tmp_project_config(tmp_path: Path, fake_worktree: Path) -> ProjectConfig:
    """A fully valid ProjectConfig that references tmp dirs — no real repo required."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)

    profiles = {
        "cheap_planner": _make_profile(
            name="cheap_planner",
            model="z-ai/glm-4.7-flash",
            input_cost=0.06,
            output_cost=0.40,
            use_for=["plan", "clarify", "summarize"],
        ),
        "cheap_coder": _make_profile(
            name="cheap_coder",
            provider="openrouter",
            model="qwen/qwen3-coder-30b-a3b",
            input_cost=0.07,
            output_cost=0.27,
            use_for=["implement_simple", "fix_simple"],
        ),
        "executor": _make_profile(
            name="executor",
            model="z-ai/glm-4.7",
            input_cost=0.38,
            output_cost=1.74,
            use_for=["implement", "implement_complex"],
        ),
        "debugger": _make_profile(
            name="debugger",
            provider="openrouter",
            model="deepseek/deepseek-v3.2",
            input_cost=0.25,
            output_cost=0.40,
            use_for=["diagnose", "debug", "review"],
        ),
    }

    return ProjectConfig(
        name="jungle",
        github=GithubConfig(owner="pinestraw", repo="jungle"),
        paths=PathsConfig(repo_root=repo_root, worktree_root=worktree_root),
        commands=CommandsConfig(
            create_worktree="make agent-worktree-create agent={agent} slug={slug}",
            preflight="make mandate-preflight MANDATE_SLUG={slug}",
        ),
        models=ModelsConfig(
            default=_make_profile(model="z-ai/glm-4.7-flash"),
            profiles=profiles,
        ),
        context_budgets=ContextBudgetsConfig(),
        retry_policy=RetryPolicyConfig(),
    )


@pytest.fixture
def fake_run_dir(tmp_path: Path) -> Path:
    """A temporary run directory pre-populated with agent artifact stubs."""
    run_dir = tmp_path / "state" / "jungle" / "runs" / "oc1"
    run_dir.mkdir(parents=True)

    (run_dir / "mandate.md").write_text(
        "# Mandate\n\nImplement the feature as described in GitHub issue #45.",
        encoding="utf-8",
    )
    (run_dir / "decisions.md").write_text("# Decisions\n\n", encoding="utf-8")
    (run_dir / "questions.md").write_text("# Questions\n\n", encoding="utf-8")
    (run_dir / "running_summary.md").write_text(
        "# Running Summary\n\nStarted implementation.",
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture
def fake_agent_state(tmp_path: Path, fake_worktree: Path, fake_run_dir: Path) -> dict[str, Any]:
    """Minimal agent state dict that matches what Cascade saves on `claim`."""
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        f"""
name: jungle
github:
  owner: pinestraw
  repo: jungle
paths:
  repo_root: {tmp_path / "repo"}
  worktree_root: {tmp_path / "worktrees"}
commands:
  create_worktree: echo create
  preflight: echo preflight-ok
models:
  default:
    provider: openrouter
    model: z-ai/glm-4.7-flash
  profiles:
    cheap_planner:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      input_cost_per_million: 0.06
      output_cost_per_million: 0.40
      use_for:
        - plan
        - summarize
    executor:
      provider: openrouter
      model: z-ai/glm-4.7
      input_cost_per_million: 0.38
      output_cost_per_million: 1.74
      use_for:
        - implement
""".strip()
        + "\n",
        encoding="utf-8",
    )

    state: dict[str, Any] = {
        "project": "jungle",
        "agent": "oc1",
        "issue": 45,
        "title": "Test Feature",
        "slug": "test-feature",
        "engine": "opencode",
        "model": "openrouter/z-ai/glm-4.7-flash",
        "state": "claimed",
        "worktree": str(fake_worktree),
        "run_dir": str(fake_run_dir),
        "project_file": str(project_file),
        "mandate": "Implement the test feature safely.",
        "running_summary": "Working on implementation.",
        "decisions": [],
        "questions": [],
    }

    agents_dir = tmp_path / "state" / "jungle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "oc1.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    return state


@pytest.fixture
def forbid_opencode_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch run_command so that any invocation containing 'opencode' raises.

    Use this fixture in tests for deterministic commands to prove they never
    reach out to OpenCode.
    """
    import cascade.shell as shell_module

    original_run_command = shell_module.run_command

    def _guarded_run(cmd: str, cwd: Path | None = None):  # type: ignore[return]
        if "opencode" in cmd.lower():
            raise AssertionError(
                f"Deterministic command must not invoke OpenCode. Got: {cmd!r}"
            )
        return original_run_command(cmd, cwd=cwd)

    monkeypatch.setattr(shell_module, "run_command", _guarded_run)

    import cascade.cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "ensure_opencode_available",
        lambda: (_ for _ in ()).throw(
            AssertionError("Deterministic command must not check for OpenCode availability.")
        ),
    )
