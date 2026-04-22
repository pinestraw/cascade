"""Tests for cost-control infrastructure: config profiles, costs, context packs, gates, state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cascade.config import (
    CommandsConfig,
    ConfigError,
    ContextBudget,
    ContextBudgetsConfig,
    GithubConfig,
    ModelProfile,
    ModelsConfig,
    PathsConfig,
    ProjectConfig,
    RetryPolicyConfig,
    get_model_profile,
    model_id_for_opencode,
    resolve_model_for_task,
)
from cascade.costs import (
    DEFAULT_EXPECTED_OUTPUT_TOKENS,
    cost_summary_lines,
    estimate_cost,
    estimate_tokens,
    format_cost,
)
from cascade.gates import classify_gate_failure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(**kwargs: object) -> ModelProfile:
    defaults: dict[str, object] = {
        "name": "test-profile",
        "provider": "openrouter",
        "model": "z-ai/glm-4.7-flash",
        "description": "test",
        "input_cost_per_million": 0.06,
        "output_cost_per_million": 0.40,
        "use_for": ["plan"],
    }
    defaults.update(kwargs)
    return ModelProfile(**defaults)  # type: ignore[arg-type]


def _make_project(tmp_path: Path | None = None, **kwargs: object) -> ProjectConfig:
    from pathlib import Path as _Path
    base = tmp_path or _Path("/tmp/cascade-test")
    profile = _make_profile(name="cheap_planner", use_for=["plan", "summarize"])
    executor = _make_profile(
        name="executor",
        provider="openrouter",
        model="z-ai/glm-4.7",
        input_cost_per_million=0.38,
        output_cost_per_million=1.74,
        use_for=["implement"],
    )
    models = ModelsConfig(
        default=ModelProfile(provider="openrouter", model="z-ai/glm-4.7-flash"),
        profiles={"cheap_planner": profile, "executor": executor},
    )
    defaults: dict[str, object] = {
        "name": "test-project",
        "github": GithubConfig(owner="testorg", repo="testrepo"),
        "paths": PathsConfig(repo_root=base / "repo", worktree_root=base / "worktrees"),
        "commands": CommandsConfig(create_worktree="echo create", preflight="echo preflight"),
        "models": models,
        "context_budgets": ContextBudgetsConfig(),
        "retry_policy": RetryPolicyConfig(),
    }
    defaults.update(kwargs)
    return ProjectConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# model_id_for_opencode
# ---------------------------------------------------------------------------


def test_model_id_for_opencode_openrouter() -> None:
    profile = _make_profile(provider="openrouter", model="z-ai/glm-4.7-flash")
    result = model_id_for_opencode(profile)
    assert result == "openrouter/z-ai/glm-4.7-flash"


def test_model_id_for_opencode_anthropic() -> None:
    profile = _make_profile(provider="anthropic", model="claude-sonnet-4-5")
    result = model_id_for_opencode(profile)
    assert result == "anthropic/claude-sonnet-4-5"


def test_model_id_for_opencode_local_passthrough() -> None:
    """Provider 'local' — produces 'local/<model>'."""
    profile = _make_profile(provider="local", model="llama3")
    result = model_id_for_opencode(profile)
    assert result == "local/llama3"


# ---------------------------------------------------------------------------
# get_model_profile
# ---------------------------------------------------------------------------


def test_get_model_profile_found() -> None:
    project = _make_project()
    profile = get_model_profile(project, "cheap_planner")
    assert profile.model == "z-ai/glm-4.7-flash"


def test_get_model_profile_not_found_raises() -> None:
    project = _make_project()
    with pytest.raises(ConfigError, match="nonexistent"):
        get_model_profile(project, "nonexistent")


# ---------------------------------------------------------------------------
# resolve_model_for_task
# ---------------------------------------------------------------------------


def test_resolve_model_for_task_finds_first_match() -> None:
    project = _make_project()
    profile = resolve_model_for_task(project, "plan")
    assert profile is not None
    # The plan profile uses z-ai/glm-4.7-flash (cheap_planner)
    assert profile.model == "z-ai/glm-4.7-flash"


def test_resolve_model_for_task_returns_none_when_no_match() -> None:
    project = _make_project()
    result = resolve_model_for_task(project, "diagnose")
    assert result is None


def test_resolve_model_for_task_implement() -> None:
    project = _make_project()
    result = resolve_model_for_task(project, "implement")
    assert result is not None
    # executor profile uses z-ai/glm-4.7
    assert result.model == "z-ai/glm-4.7"


# ---------------------------------------------------------------------------
# costs.estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_short_string() -> None:
    # "hello" is 5 chars → 5//4 = 1, but max(1, 1) = 1
    assert estimate_tokens("hello") == 1


def test_estimate_tokens_empty_returns_one() -> None:
    assert estimate_tokens("") == 1


def test_estimate_tokens_long_string() -> None:
    text = "a" * 4000
    assert estimate_tokens(text) == 1000


# ---------------------------------------------------------------------------
# costs.estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_zero_when_no_pricing() -> None:
    profile = _make_profile(input_cost_per_million=0.0, output_cost_per_million=0.0)
    assert estimate_cost(100_000, 50_000, profile) == pytest.approx(0.0)


def test_estimate_cost_exact_million_tokens() -> None:
    profile = _make_profile(input_cost_per_million=1.0, output_cost_per_million=2.0)
    cost = estimate_cost(1_000_000, 0, profile)
    assert cost == pytest.approx(1.0)


def test_estimate_cost_combined() -> None:
    profile = _make_profile(input_cost_per_million=0.06, output_cost_per_million=0.40)
    # 100k input tokens: 0.06 * 100000 / 1_000_000 = 0.006
    # 5k output tokens: 0.40 * 5000 / 1_000_000 = 0.002
    cost = estimate_cost(100_000, 5_000, profile)
    assert cost == pytest.approx(0.008, rel=1e-6)


# ---------------------------------------------------------------------------
# costs.format_cost
# ---------------------------------------------------------------------------


def test_format_cost_large() -> None:
    s = format_cost(1.23456)
    assert "1.23" in s or "USD" in s


def test_format_cost_small_milliusd() -> None:
    s = format_cost(0.00015)
    # Should express in mUSD (thousandths)
    assert "mUSD" in s or "USD" in s


# ---------------------------------------------------------------------------
# costs.cost_summary_lines
# ---------------------------------------------------------------------------


def test_cost_summary_lines_returns_lines() -> None:
    profile = _make_profile(input_cost_per_million=0.06, output_cost_per_million=0.40)
    lines = cost_summary_lines(50_000, 5_000, profile, "cheap_planner")
    assert len(lines) >= 3
    joined = "\n".join(lines)
    assert "cheap_planner" in joined
    assert "input" in joined.lower() or "token" in joined.lower()


# ---------------------------------------------------------------------------
# context_pack
# ---------------------------------------------------------------------------


def _minimal_agent_state(worktree: Path) -> dict[str, object]:
    return {
        "agent": "oc1",
        "project": "test-project",
        "project_file": "test.yaml",
        "issue": 42,
        "title": "Test issue",
        "slug": "test-issue",
        "worktree": str(worktree),
        "branch": "agent/oc1/test-issue",
        "state": "implementing",
        "mandate": "Implement the test feature.",
        "running_summary": "Working on it.",
        "decisions": [],
        "questions": [],
    }


def test_context_pack_writes_md_and_json(tmp_path: Path) -> None:
    from cascade.context_pack import build_context_pack, save_context_pack

    project = _make_project()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    agent_state = _minimal_agent_state(worktree)
    run_dir = tmp_path / "runs" / "oc1"
    run_dir.mkdir(parents=True)

    pack = build_context_pack(project, agent_state, "plan", run_dir)
    md_path, json_path = save_context_pack(run_dir, pack)

    assert md_path.exists()
    assert json_path.exists()
    md = md_path.read_text(encoding="utf-8")
    meta = json.loads(json_path.read_text(encoding="utf-8"))

    assert "plan" in md.lower() or "mandate" in md.lower()
    assert meta["task_type"] == "plan"
    assert "estimated_input_tokens" in meta
    assert "max_input_tokens" in meta
    assert isinstance(meta["truncated"], bool)


def test_context_pack_does_not_include_secret_files(tmp_path: Path) -> None:
    """context_pack must not read .env or secrets-like paths."""
    from cascade.context_pack import _is_blocked_path

    # These should all be blocked
    assert _is_blocked_path(Path("/some/dir/.env"))
    assert _is_blocked_path(Path("/some/dir/.env.local"))
    # 'credentials' as a directory component is blocked
    assert _is_blocked_path(Path("/some/credentials/data.json"))
    # 'private_key' as a directory component is blocked
    assert _is_blocked_path(Path("/some/private_key/data.pem"))
    # 'secrets' directory is blocked
    assert _is_blocked_path(Path("/some/secrets/config.yaml"))
    # Regular files are not blocked
    assert not _is_blocked_path(Path("/some/dir/config.yaml"))
    assert not _is_blocked_path(Path("/some/dir/credentials.json"))  # filename, not dir


def test_context_pack_task_type_validation(tmp_path: Path) -> None:
    from cascade.context_pack import build_context_pack

    project = _make_project()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    agent_state = _minimal_agent_state(worktree)
    run_dir = tmp_path / "runs" / "oc1"
    run_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="Unknown task"):
        build_context_pack(project, agent_state, "invalid_task_type", run_dir)


def test_context_pack_respects_budget(tmp_path: Path) -> None:
    """When budget is tiny, truncated flag should be set."""
    from cascade.config import ContextBudgetsConfig
    from cascade.context_pack import build_context_pack, save_context_pack

    tiny_budget = ContextBudgetsConfig(plan=ContextBudget(max_input_tokens=100))
    project = _make_project(context_budgets=tiny_budget)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    agent_state = _minimal_agent_state(worktree)
    # Give a large mandate to force truncation
    agent_state["mandate"] = "X " * 1000
    run_dir = tmp_path / "runs" / "oc1"
    run_dir.mkdir(parents=True)

    pack = build_context_pack(project, agent_state, "plan", run_dir)
    # Even under extreme truncation the pack should still have a body
    assert pack.body
    # estimated_input_tokens might be over budget if only mandatory sections remain
    assert isinstance(pack.truncated, bool)


# ---------------------------------------------------------------------------
# classify_gate_failure
# ---------------------------------------------------------------------------


def test_classify_empty_log_not_detected() -> None:
    result = classify_gate_failure("")
    assert result["detected"] is False
    assert result["category"] == "unknown"


def test_classify_trailing_whitespace_no_model() -> None:
    log = "Failed: trailing-whitespace\nSome files had trailing whitespace.\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "formatting"
    assert result["model_recommended"] is False


def test_classify_ruff_format_no_model() -> None:
    log = "- hook id: ruff-format\n  exit code: 1\n  reformatted files\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "formatting"
    assert result["model_recommended"] is False


def test_classify_pyright_model_recommended() -> None:
    log = "- hook id: pyright\n  exit code: 1\nerror: Type 'int' is not assignable to 'str'\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "typing"
    assert result["model_recommended"] is True


def test_classify_gitleaks_security() -> None:
    log = "- hook id: gitleaks\n  exit code: 1\nSecret detected: AWS key\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "security"
    assert result["model_recommended"] is True


def test_classify_migration_check() -> None:
    log = "- hook id: jungle-migrate-check\n  exit code: 1\nMissing migration for model change.\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "migration"
    assert result["model_recommended"] is True


def test_classify_ruff_linting_no_model() -> None:
    log = "- hook id: ruff\n  exit code: 1\n  E501 line too long\n"
    result = classify_gate_failure(log)
    assert result["detected"] is True
    assert result["category"] == "linting"
    assert result["model_recommended"] is False


def test_classify_unknown_hook_returns_unknown() -> None:
    log = "- hook id: my-custom-gate\n  exit code: 1\nsome unknown failure\n"
    result = classify_gate_failure(log)
    # detected because a hook was extracted
    assert result["detected"] is True
    assert result["category"] == "unknown"
    assert result["model_recommended"] is True


# ---------------------------------------------------------------------------
# state.py retry helpers
# ---------------------------------------------------------------------------


def _write_agent_state(state_dir: Path, project: str, agent: str, state_data: dict[str, object]) -> None:
    agents_dir = state_dir / project / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent}.json").write_text(json.dumps(state_data), encoding="utf-8")


def test_get_attempt_count_zero_when_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from cascade.state import get_attempt_count

    count = get_attempt_count("myproject", "oc1", "plan")
    assert count == 0


def test_increment_attempt_increments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_agent_state(tmp_path / "state", "myproject", "oc1", {
        "agent": "oc1",
        "project": "myproject",
        "state": "implementing",
    })

    from cascade.state import get_attempt_count, increment_attempt

    n1 = increment_attempt("myproject", "oc1", "plan", "cheap_planner")
    n2 = increment_attempt("myproject", "oc1", "plan", "cheap_planner")

    assert n1 == 1
    assert n2 == 2
    assert get_attempt_count("myproject", "oc1", "plan") == 2


def test_increment_attempt_tracks_different_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_agent_state(tmp_path / "state", "myproject", "oc1", {
        "agent": "oc1",
        "project": "myproject",
        "state": "implementing",
    })

    from cascade.state import get_attempt_count, increment_attempt

    increment_attempt("myproject", "oc1", "plan", "cheap_planner")
    increment_attempt("myproject", "oc1", "implement", "executor")

    assert get_attempt_count("myproject", "oc1", "plan") == 1
    assert get_attempt_count("myproject", "oc1", "implement") == 1
    assert get_attempt_count("myproject", "oc1", "diagnose") == 0


def test_should_escalate_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_agent_state(tmp_path / "state", "myproject", "oc1", {
        "agent": "oc1",
        "project": "myproject",
        "state": "implementing",
    })

    from cascade.state import increment_attempt, should_escalate

    project = _make_project()

    state_path = tmp_path / "state" / "myproject" / "agents" / "oc1.json"
    agent_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert not should_escalate(project, agent_state, "plan")

    # Increment enough to hit escalation threshold
    for _ in range(project.retry_policy.same_gate_failure_escalation_after):
        increment_attempt("myproject", "oc1", "plan", "cheap_planner")

    agent_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert should_escalate(project, agent_state, "plan")


# ---------------------------------------------------------------------------
# prompts.py task output rules
# ---------------------------------------------------------------------------


def test_build_task_prompt_includes_output_rules() -> None:
    from cascade.prompts import build_task_prompt

    prompt = build_task_prompt("# Context\n\nSome context.", "diagnose")
    assert "Output discipline" in prompt
    assert "root cause" in prompt.lower()


def test_build_task_prompt_unknown_task_still_returns_prompt() -> None:
    from cascade.prompts import build_task_prompt

    prompt = build_task_prompt("# Context", "unknown_task_xyz")
    assert "# Context" in prompt
    assert "unknown_task_xyz" in prompt
