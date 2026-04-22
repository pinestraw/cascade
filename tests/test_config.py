from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import (
    ConfigError,
    ContextBudget,
    ContextBudgetsConfig,
    ModelProfile,
    ModelsConfig,
    ProjectConfig,
    RetryPolicyConfig,
    get_model_profile,
    load_project_config,
    model_id_for_opencode,
    resolve_model_for_task,
)


# ---------------------------------------------------------------------------
# Basic YAML loading
# ---------------------------------------------------------------------------


def test_load_project_config_with_minimal_valid_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: demo
github:
  owner: acme
  repo: widget
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )

    project = load_project_config(project_file)

    assert project.name == "demo"
    assert project.github.owner == "acme"
    assert project.paths.repo_root == (tmp_path / "repo").resolve()


def test_load_project_config_fails_when_required_fields_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: demo
github:
  owner: acme
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_project_config(project_file)

    assert "Invalid project configuration" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Model profile parsing via YAML
# ---------------------------------------------------------------------------


def _write_yaml_with_profiles(project_file: Path, extra_yaml: str = "") -> None:
    project_file.write_text(
        f"""
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
    input_cost_per_million: 0.06
    output_cost_per_million: 0.40
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
        - implement_complex
{extra_yaml}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_model_profiles_parsed_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(project_file)

    project = load_project_config(project_file)

    assert "cheap_planner" in project.models.profiles
    assert "executor" in project.models.profiles

    cp = project.models.profiles["cheap_planner"]
    assert cp.model == "z-ai/glm-4.7-flash"
    assert cp.input_cost_per_million == pytest.approx(0.06)
    assert "plan" in cp.use_for

    ex = project.models.profiles["executor"]
    assert ex.model == "z-ai/glm-4.7"
    assert ex.output_cost_per_million == pytest.approx(1.74)


def test_model_profile_use_for_is_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(project_file)

    project = load_project_config(project_file)
    assert isinstance(project.models.profiles["cheap_planner"].use_for, list)


def test_model_profile_missing_costs_defaults_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
name: demo
github:
  owner: acme
  repo: widget
paths:
  repo_root: ./repo
  worktree_root: ./worktrees
commands:
  create_worktree: echo create
models:
  profiles:
    simple:
      provider: openrouter
      model: z-ai/glm-4.7-flash
""".strip()
        + "\n",
        encoding="utf-8",
    )

    project = load_project_config(project_file)
    profile = project.models.profiles["simple"]
    assert profile.input_cost_per_million == 0.0
    assert profile.output_cost_per_million == 0.0


# ---------------------------------------------------------------------------
# Context budgets
# ---------------------------------------------------------------------------


def test_context_budgets_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(project_file)

    project = load_project_config(project_file)

    plan_budget = project.context_budgets.for_task("plan")
    assert isinstance(plan_budget, ContextBudget)
    assert plan_budget.max_input_tokens > 0

    implement_budget = project.context_budgets.for_task("implement")
    # implement should have a larger budget than plan
    assert implement_budget.max_input_tokens >= plan_budget.max_input_tokens


def test_context_budgets_custom_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(
        project_file,
        extra_yaml="""
context_budgets:
  plan:
    max_input_tokens: 12345
    include_full_diff: true
""",
    )

    project = load_project_config(project_file)
    plan_budget = project.context_budgets.for_task("plan")
    assert plan_budget.max_input_tokens == 12345
    assert plan_budget.include_full_diff is True


def test_context_budgets_for_unknown_task_returns_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(project_file)

    project = load_project_config(project_file)
    fallback = project.context_budgets.for_task("totally_unknown_task_type")
    assert isinstance(fallback, ContextBudget)
    assert fallback.max_input_tokens > 0


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_retry_policy_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(project_file)

    project = load_project_config(project_file)
    rp = project.retry_policy
    assert isinstance(rp, RetryPolicyConfig)
    assert rp.cheap_coder_max_attempts >= 1
    assert rp.executor_max_attempts >= 1
    assert rp.same_gate_failure_escalation_after >= 1


def test_retry_policy_custom_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    project_file = tmp_path / "project.yaml"
    _write_yaml_with_profiles(
        project_file,
        extra_yaml="""
retry_policy:
  cheap_coder_max_attempts: 3
  executor_max_attempts: 4
  same_gate_failure_escalation_after: 1
""",
    )

    project = load_project_config(project_file)
    assert project.retry_policy.cheap_coder_max_attempts == 3
    assert project.retry_policy.executor_max_attempts == 4
    assert project.retry_policy.same_gate_failure_escalation_after == 1


def test_retry_policy_max_attempts_for_profile() -> None:
    rp = RetryPolicyConfig(cheap_coder_max_attempts=5, executor_max_attempts=3)
    assert rp.max_attempts_for_profile("cheap_coder") == 5
    assert rp.max_attempts_for_profile("executor") == 3
    # Unknown profile falls back to 1
    assert rp.max_attempts_for_profile("nonexistent_profile") == 1


# ---------------------------------------------------------------------------
# get_model_profile helper
# ---------------------------------------------------------------------------


def _make_project_with_profiles() -> ProjectConfig:
    return ProjectConfig(
        name="test",
        github=__import__("cascade.config", fromlist=["GithubConfig"]).GithubConfig(owner="x", repo="y"),
        paths=__import__("cascade.config", fromlist=["PathsConfig"]).PathsConfig(
            repo_root=Path("/tmp/repo"), worktree_root=Path("/tmp/worktrees")
        ),
        commands=__import__("cascade.config", fromlist=["CommandsConfig"]).CommandsConfig(
            create_worktree="echo create"
        ),
        models=ModelsConfig(
            profiles={
                "cheap_planner": ModelProfile(
                    provider="openrouter",
                    model="z-ai/glm-4.7-flash",
                    use_for=["plan", "summarize"],
                ),
                "executor": ModelProfile(
                    provider="openrouter",
                    model="z-ai/glm-4.7",
                    input_cost_per_million=0.38,
                    output_cost_per_million=1.74,
                    use_for=["implement"],
                ),
            }
        ),
    )


def test_get_model_profile_returns_named_profile() -> None:
    project = _make_project_with_profiles()
    profile = get_model_profile(project, "cheap_planner")
    assert profile.model == "z-ai/glm-4.7-flash"


def test_get_model_profile_unknown_raises_config_error() -> None:
    project = _make_project_with_profiles()
    with pytest.raises(ConfigError, match="cheap_planner"):
        # Wrong name — deliberately use a name that contains info about available ones
        get_model_profile(project, "this_profile_does_not_exist")


def test_get_model_profile_error_lists_available_profiles() -> None:
    project = _make_project_with_profiles()
    with pytest.raises(ConfigError) as excinfo:
        get_model_profile(project, "nonexistent")
    # Error should mention what's available
    assert "cheap_planner" in str(excinfo.value) or "executor" in str(excinfo.value)


# ---------------------------------------------------------------------------
# resolve_model_for_task helper
# ---------------------------------------------------------------------------


def test_resolve_model_for_task_finds_plan_profile() -> None:
    project = _make_project_with_profiles()
    profile = resolve_model_for_task(project, "plan")
    assert profile is not None
    assert profile.model == "z-ai/glm-4.7-flash"


def test_resolve_model_for_task_finds_implement_profile() -> None:
    project = _make_project_with_profiles()
    profile = resolve_model_for_task(project, "implement")
    assert profile is not None
    assert profile.model == "z-ai/glm-4.7"


def test_resolve_model_for_task_returns_none_when_no_match() -> None:
    project = _make_project_with_profiles()
    result = resolve_model_for_task(project, "diagnose")
    assert result is None


# ---------------------------------------------------------------------------
# model_id_for_opencode helper
# ---------------------------------------------------------------------------


def test_model_id_for_opencode_openrouter_profile() -> None:
    profile = ModelProfile(provider="openrouter", model="z-ai/glm-4.7-flash")
    assert model_id_for_opencode(profile) == "openrouter/z-ai/glm-4.7-flash"


def test_model_id_for_opencode_openrouter_deepseek() -> None:
    """Ensure multi-part model IDs like deepseek/deepseek-v3.2 are preserved."""
    profile = ModelProfile(provider="openrouter", model="deepseek/deepseek-v3.2")
    assert model_id_for_opencode(profile) == "openrouter/deepseek/deepseek-v3.2"


def test_model_id_for_opencode_anthropic_profile() -> None:
    profile = ModelProfile(provider="anthropic", model="claude-sonnet-4-5")
    assert model_id_for_opencode(profile) == "anthropic/claude-sonnet-4-5"


def test_model_id_for_opencode_local_profile_no_extra_prefix() -> None:
    """Local/ollama profiles should not get a double prefix."""
    profile = ModelProfile(provider="local", model="llama3")
    result = model_id_for_opencode(profile)
    assert result == "local/llama3"


def test_model_id_for_opencode_uses_lowercase_provider() -> None:
    """Provider name should be lowercased before constructing the ID."""
    profile = ModelProfile(provider="OpenRouter", model="z-ai/glm-4.7-flash")
    result = model_id_for_opencode(profile)
    assert result.startswith("openrouter/")
