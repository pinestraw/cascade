from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class GithubConfig(BaseModel):
    owner: str
    repo: str
    project_name: str | None = None


class PathsConfig(BaseModel):
    workspace_root: Path | None = None
    repo_root: Path
    worktree_root: Path
    secrets_root: Path | None = None


class InstructionsConfig(BaseModel):
    files: list[str] = Field(default_factory=list)


class CommandsConfig(BaseModel):
    create_worktree: str
    mandate_start: str | None = None
    start_mandate: str | None = None
    init_mandate: str | None = None
    preflight: str | None = None
    done: str | None = None
    status: str | None = None


class BranchesConfig(BaseModel):
    active_branch: str | None = None
    base: str | None = None
    agent_branch_template: str | None = None


# ---------------------------------------------------------------------------
# Model profiles (extended)
# ---------------------------------------------------------------------------

TaskType = Literal[
    "plan",
    "clarify",
    "summarize",
    "implement",
    "implement_simple",
    "implement_complex",
    "fix",
    "fix_simple",
    "diagnose",
    "debug",
    "review",
]


class ModelProfile(BaseModel):
    provider: str
    model: str
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    use_for: list[str] = Field(default_factory=list)


class ModelsConfig(BaseModel):
    default: ModelProfile | None = None
    cheap: ModelProfile | None = None
    strong: ModelProfile | None = None
    local: ModelProfile | None = None
    # Named model profiles keyed by profile name (e.g. cheap_planner, executor)
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Context budgets
# ---------------------------------------------------------------------------


class ContextBudget(BaseModel):
    max_input_tokens: int = 80000
    include_full_diff: bool = False
    include_diff_stat: bool = True
    include_logs_tail_lines: int = 150
    include_instruction_files: bool = True
    include_full_transcript: bool = False


_DEFAULT_BUDGETS: dict[str, ContextBudget] = {
    "plan": ContextBudget(max_input_tokens=50000, include_logs_tail_lines=150, include_instruction_files=True),
    "implement": ContextBudget(max_input_tokens=120000, include_logs_tail_lines=150, include_instruction_files=True),
    "diagnose": ContextBudget(max_input_tokens=60000, include_logs_tail_lines=300, include_instruction_files=False),
    "fix": ContextBudget(max_input_tokens=80000, include_logs_tail_lines=300, include_instruction_files=False),
    "review": ContextBudget(max_input_tokens=100000, include_logs_tail_lines=200, include_instruction_files=True),
    "summarize": ContextBudget(max_input_tokens=40000, include_logs_tail_lines=100, include_instruction_files=False),
}


class ContextBudgetsConfig(BaseModel):
    plan: ContextBudget = Field(default_factory=lambda: _DEFAULT_BUDGETS["plan"])
    implement: ContextBudget = Field(default_factory=lambda: _DEFAULT_BUDGETS["implement"])
    diagnose: ContextBudget = Field(default_factory=lambda: _DEFAULT_BUDGETS["diagnose"])
    fix: ContextBudget = Field(default_factory=lambda: _DEFAULT_BUDGETS["fix"])
    review: ContextBudget = Field(default_factory=lambda: _DEFAULT_BUDGETS["review"])
    summarize: ContextBudget = Field(default_factory=lambda: _DEFAULT_BUDGETS["summarize"])

    def for_task(self, task_type: str) -> ContextBudget:
        budget = getattr(self, task_type, None)
        if isinstance(budget, ContextBudget):
            return budget
        return _DEFAULT_BUDGETS.get(task_type, ContextBudget())


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class RetryPolicyConfig(BaseModel):
    cheap_coder_max_attempts: int = 2
    executor_max_attempts: int = 2
    debugger_max_attempts: int = 1
    same_gate_failure_escalation_after: int = 2

    def max_attempts_for_profile(self, profile_name: str) -> int:
        mapping: dict[str, int] = {
            "cheap_coder": self.cheap_coder_max_attempts,
            "executor": self.executor_max_attempts,
            "debugger": self.debugger_max_attempts,
        }
        return mapping.get(profile_name, 1)


# ---------------------------------------------------------------------------
# Project config
# ---------------------------------------------------------------------------


class ProjectConfig(BaseModel):
    name: str
    default_active_branch: str | None = None
    github: GithubConfig
    paths: PathsConfig
    related_repos: dict[str, Path] = Field(default_factory=dict)
    instructions: InstructionsConfig = Field(default_factory=InstructionsConfig)
    commands: CommandsConfig
    branches: BranchesConfig = Field(default_factory=BranchesConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    context_budgets: ContextBudgetsConfig = Field(default_factory=ContextBudgetsConfig)
    retry_policy: RetryPolicyConfig = Field(default_factory=RetryPolicyConfig)

    @model_validator(mode="after")
    def validate_required_fields(self) -> "ProjectConfig":
        if not self.name.strip():
            raise ValueError("Project name cannot be empty.")
        return self


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Workspace validation helpers
# ---------------------------------------------------------------------------

# Directory names that suggest an overly broad workspace_root.
_BROAD_WORKSPACE_NAMES = frozenset({
    "github-projects",
    "documents",
    "desktop",
    "projects",
    "code",
    "dev",
    "src",
    "workspace",
})


@dataclass(frozen=True)
class ValidationResult:
    """Result of a single path validation check."""

    key: str
    path: Path
    status: str  # "ok" | "warn" | "fail"
    message: str


def resolve_workspace_root(project: "ProjectConfig") -> Path | None:
    """Return the resolved workspace_root, or None if not configured."""
    if project.paths.workspace_root is None:
        return None
    return project.paths.workspace_root.resolve()


def is_inside_workspace(path: Path, workspace_root: Path) -> bool:
    """Return True if *path* is inside (or equal to) *workspace_root*."""
    try:
        path.resolve().relative_to(workspace_root.resolve())
        return True
    except ValueError:
        return False


def workspace_root_is_broad(workspace_root: Path) -> bool:
    """Heuristic: return True if the workspace_root looks overly broad."""
    home = Path.home()
    resolved = workspace_root.resolve()
    if resolved == home:
        return True
    name_lower = resolved.name.lower()
    return name_lower in _BROAD_WORKSPACE_NAMES


def validate_project_paths(project: "ProjectConfig") -> list[ValidationResult]:
    """Validate all configured paths against the workspace boundary.

    Returns a list of ValidationResult items (ok/warn/fail) for every path.
    """
    results: list[ValidationResult] = []
    workspace = resolve_workspace_root(project)

    if workspace is not None:
        exists = workspace.exists()
        results.append(ValidationResult(
            key="workspace_root",
            path=workspace,
            status="ok" if exists else "fail",
            message=str(workspace) if exists else f"workspace_root does not exist: {workspace}",
        ))

        if workspace_root_is_broad(workspace):
            results.append(ValidationResult(
                key="workspace_root_broad",
                path=workspace,
                status="warn",
                message=(
                    f"workspace_root appears broad ({workspace.name!r}); "
                    "prefer a dedicated workspace such as 'instica-workspace'."
                ),
            ))

    def _check(key: str, path: Path, *, required: bool) -> None:
        exists = path.exists()
        inside = workspace is None or is_inside_workspace(path, workspace)
        if not inside:
            results.append(ValidationResult(
                key=key,
                path=path,
                status="fail",
                message=f"{key} ({path}) is outside workspace_root ({workspace}). Escape paths are not allowed.",
            ))
            return
        if not exists:
            status = "fail" if required else "warn"
            results.append(ValidationResult(
                key=key,
                path=path,
                status=status,
                message=f"{key} does not exist: {path}",
            ))
            return
        results.append(ValidationResult(
            key=key,
            path=path,
            status="ok",
            message=str(path),
        ))

    _check("repo_root", project.paths.repo_root, required=True)
    _check("worktree_root", project.paths.worktree_root, required=False)
    if project.paths.secrets_root is not None:
        _check("secrets_root", project.paths.secrets_root, required=False)

    for name, related_path in project.related_repos.items():
        _check(f"related_repos.{name}", related_path, required=False)

    return results


def load_project_config(project_file: Path) -> ProjectConfig:
    try:
        raw_data = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Project file not found: {project_file}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in project file {project_file}: {exc}") from exc

    try:
        project = ProjectConfig.model_validate(raw_data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid project configuration in {project_file}:\n{exc}") from exc

    return resolve_project_paths(project)


def resolve_project_paths(project: ProjectConfig) -> ProjectConfig:
    """Resolve all paths in the project config.

    When workspace_root is configured:
      - workspace_root is resolved relative to CWD.
      - repo_root, worktree_root, secrets_root, and related_repos values are
        resolved relative to workspace_root (not CWD).

    When workspace_root is absent (legacy configs):
      - All paths are resolved relative to CWD as before.
    """
    workspace_root = project.paths.workspace_root
    if workspace_root is not None:
        resolved_ws = workspace_root.resolve()
        project.paths.workspace_root = resolved_ws
        project.paths.repo_root = (resolved_ws / project.paths.repo_root).resolve()
        project.paths.worktree_root = (resolved_ws / project.paths.worktree_root).resolve()
        if project.paths.secrets_root is not None:
            project.paths.secrets_root = (resolved_ws / project.paths.secrets_root).resolve()
        project.related_repos = {
            name: (resolved_ws / rel_path).resolve()
            for name, rel_path in project.related_repos.items()
        }
    else:
        project.paths.repo_root = project.paths.repo_root.resolve()
        project.paths.worktree_root = project.paths.worktree_root.resolve()
        if project.paths.secrets_root is not None:
            project.paths.secrets_root = project.paths.secrets_root.resolve()
        project.related_repos = {
            name: rel_path.resolve()
            for name, rel_path in project.related_repos.items()
        }
    return project


def instruction_file_paths(project: ProjectConfig) -> list[Path]:
    resolved_paths: list[Path] = []
    for configured_path in project.instructions.files:
        direct_path = project.paths.repo_root / configured_path
        github_path = project.paths.repo_root / ".github" / configured_path
        if direct_path.exists():
            resolved_paths.append(direct_path)
        elif github_path.exists():
            resolved_paths.append(github_path)
        else:
            resolved_paths.append(direct_path)
    return resolved_paths


# ---------------------------------------------------------------------------
# Model profile helpers
# ---------------------------------------------------------------------------


def get_model_profile(project: ProjectConfig, profile_name: str) -> ModelProfile:
    """Return the named model profile, or raise ConfigError if not found."""
    profile = project.models.profiles.get(profile_name)
    if profile is not None:
        return profile
    # Fall back to legacy slots
    legacy = getattr(project.models, profile_name, None)
    if isinstance(legacy, ModelProfile):
        return legacy
    raise ConfigError(
        f"Model profile '{profile_name}' not found in project config. "
        f"Available profiles: {list(project.models.profiles.keys())}"
    )


def resolve_model_for_task(project: ProjectConfig, task_type: str) -> ModelProfile | None:
    """Find the first named profile whose use_for list covers task_type.

    Returns None if no profile matches; callers should fall back to the default.
    """
    for profile in project.models.profiles.values():
        if task_type in profile.use_for:
            return profile
    return None


def model_id_for_opencode(profile: ModelProfile) -> str:
    """Return the OpenCode/OpenRouter model string for a profile.

    For OpenRouter profiles this produces 'openrouter/<provider>/<model>'.
    For others it returns '<provider>/<model>' verbatim.
    """
    provider = profile.provider.lower()
    if provider == "openrouter":
        return f"openrouter/{profile.model}"
    return f"{provider}/{profile.model}"
