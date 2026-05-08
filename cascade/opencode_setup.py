from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_PATH_EXPORT_BLOCK = (
    "\n# Added by Cascade OpenCode setup\n"
    "export PATH=\"$HOME/.local/bin:$PATH\"\n"
)

_BASH_PROFILE_SOURCE_BLOCK = (
    "\n# Added by Cascade OpenCode setup\n"
    "if [ -f \"$HOME/.bashrc\" ]; then\n"
    "  . \"$HOME/.bashrc\"\n"
    "fi\n"
)


@dataclass(frozen=True)
class OpenCodePathSetupResult:
    resolved_binary_path: Path | None
    resolved_from: str
    symlink_path: Path
    symlink_created: bool
    symlink_updated: bool
    symlink_already_correct: bool
    symlink_blocked: bool
    bashrc_updated: bool
    bash_profile_updated: bool
    bash_profile_sources_bashrc_added: bool


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _find_opencode_under_home(home: Path) -> Path | None:
    candidates: list[Path] = []
    for path in home.rglob("opencode"):
        if _is_executable_file(path):
            candidates.append(path)
    if not candidates:
        return None
    # Deterministic: pick lexicographically first candidate.
    return sorted(candidates, key=lambda value: str(value))[0]


def resolve_opencode_binary(home: Path) -> tuple[Path | None, str]:
    on_path = shutil.which("opencode")
    if on_path is not None:
        return Path(on_path).resolve(), "PATH"

    found = _find_opencode_under_home(home)
    if found is not None:
        return found.resolve(), "home-scan"
    return None, "missing"


def _contains_local_bin_path_export(text: str) -> bool:
    normalized = text.replace(" ", "")
    return "$HOME/.local/bin" in normalized or "${HOME}/.local/bin" in normalized


def _contains_bashrc_source(text: str) -> bool:
    return ".bashrc" in text


def _ensure_file(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def _append_block_if_missing(path: Path, block: str, *, predicate: Callable[[str], bool]) -> bool:
    _ensure_file(path)
    original = path.read_text(encoding="utf-8")
    if predicate(original):
        return False
    updated = original + block
    path.write_text(updated, encoding="utf-8")
    return True


def _ensure_local_bin_symlink(home: Path, target_binary: Path) -> tuple[Path, bool, bool, bool, bool]:
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    symlink_path = local_bin / "opencode"

    if symlink_path.is_symlink():
        current_target = symlink_path.resolve()
        if current_target == target_binary:
            return symlink_path, False, False, True, False
        symlink_path.unlink()
        symlink_path.symlink_to(target_binary)
        return symlink_path, False, True, False, False

    if symlink_path.exists():
        # Keep existing non-symlink binaries safe; do not clobber.
        if _is_executable_file(symlink_path):
            return symlink_path, False, False, True, True
        return symlink_path, False, False, False, True

    symlink_path.symlink_to(target_binary)
    return symlink_path, True, False, False, False


def ensure_opencode_host_path_setup(home: Path | None = None) -> OpenCodePathSetupResult:
    resolved_home = home or Path.home()
    binary_path, resolved_from = resolve_opencode_binary(resolved_home)

    symlink_path = resolved_home / ".local" / "bin" / "opencode"
    symlink_created = False
    symlink_updated = False
    symlink_already_correct = False
    symlink_blocked = False
    bashrc_updated = False
    bash_profile_updated = False
    bash_profile_sources_bashrc_added = False

    bashrc_path = resolved_home / ".bashrc"
    bash_profile_path = resolved_home / ".bash_profile"

    bashrc_updated = _append_block_if_missing(
        bashrc_path,
        _PATH_EXPORT_BLOCK,
        predicate=_contains_local_bin_path_export,
    )
    bash_profile_updated = _append_block_if_missing(
        bash_profile_path,
        _PATH_EXPORT_BLOCK,
        predicate=_contains_local_bin_path_export,
    )
    bash_profile_sources_bashrc_added = _append_block_if_missing(
        bash_profile_path,
        _BASH_PROFILE_SOURCE_BLOCK,
        predicate=_contains_bashrc_source,
    )

    if binary_path is not None:
        symlink_path, symlink_created, symlink_updated, symlink_already_correct, symlink_blocked = (
            _ensure_local_bin_symlink(resolved_home, binary_path)
        )

    return OpenCodePathSetupResult(
        resolved_binary_path=binary_path,
        resolved_from=resolved_from,
        symlink_path=symlink_path,
        symlink_created=symlink_created,
        symlink_updated=symlink_updated,
        symlink_already_correct=symlink_already_correct,
        symlink_blocked=symlink_blocked,
        bashrc_updated=bashrc_updated,
        bash_profile_updated=bash_profile_updated,
        bash_profile_sources_bashrc_added=bash_profile_sources_bashrc_added,
    )
