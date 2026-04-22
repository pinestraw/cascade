from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class GithubConfig(BaseModel):
    owner: str
    repo: str
    project_name: str | None = None


class PathsConfig(BaseModel):
    repo_root: Path
    worktree_root: Path
    secrets_root: Path | None = None


class InstructionsConfig(BaseModel):
    files: list[str] = Field(default_factory=list)


class CommandsConfig(BaseModel):
    create_worktree: str
    preflight: str | None = None
    done: str | None = None
    status: str | None = None


class BranchesConfig(BaseModel):
    agent_branch_template: str | None = None


class ModelProfile(BaseModel):
    provider: str
    model: str


class ModelsConfig(BaseModel):
    default: ModelProfile | None = None
    cheap: ModelProfile | None = None
    strong: ModelProfile | None = None
    local: ModelProfile | None = None


class ProjectConfig(BaseModel):
    name: str
    github: GithubConfig
    paths: PathsConfig
    instructions: InstructionsConfig = Field(default_factory=InstructionsConfig)
    commands: CommandsConfig
    branches: BranchesConfig = Field(default_factory=BranchesConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)

    @model_validator(mode="after")
    def validate_required_fields(self) -> "ProjectConfig":
        if not self.name.strip():
            raise ValueError("Project name cannot be empty.")
        return self


class ConfigError(Exception):
    pass


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
    # MVP behavior: resolve configured paths relative to the current working
    # directory so `examples/jungle.yaml` can use sibling repo paths like
    # `../jungle` when the user runs `cascade` from the cascade repo root.
    project.paths.repo_root = project.paths.repo_root.resolve()
    project.paths.worktree_root = project.paths.worktree_root.resolve()
    if project.paths.secrets_root is not None:
        project.paths.secrets_root = project.paths.secrets_root.resolve()
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