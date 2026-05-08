from __future__ import annotations

import os
from pathlib import Path

from cascade import opencode_setup


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\necho opencode\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_resolve_opencode_binary_prefers_path(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    on_path = tmp_path / "bin" / "opencode"
    _make_executable(on_path)

    monkeypatch.setattr(opencode_setup.shutil, "which", lambda name: str(on_path) if name == "opencode" else None)

    resolved, source = opencode_setup.resolve_opencode_binary(home)

    assert resolved == on_path.resolve()
    assert source == "PATH"


def test_resolve_opencode_binary_scans_home_when_missing_on_path(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    opencode_binary = home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin" / "opencode"
    _make_executable(opencode_binary)

    monkeypatch.setattr(opencode_setup.shutil, "which", lambda name: None)

    resolved, source = opencode_setup.resolve_opencode_binary(home)

    assert resolved == opencode_binary.resolve()
    assert source == "home-scan"


def test_ensure_opencode_host_path_setup_creates_symlink_and_updates_shell_files(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    opencode_binary = home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin" / "opencode"
    _make_executable(opencode_binary)

    monkeypatch.setattr(opencode_setup.shutil, "which", lambda name: None)

    result = opencode_setup.ensure_opencode_host_path_setup(home)

    symlink_path = home / ".local" / "bin" / "opencode"
    bashrc = home / ".bashrc"
    bash_profile = home / ".bash_profile"

    assert result.resolved_binary_path == opencode_binary.resolve()
    assert result.symlink_path == symlink_path
    assert result.symlink_created
    assert symlink_path.is_symlink()
    assert symlink_path.resolve() == opencode_binary.resolve()

    bashrc_text = bashrc.read_text(encoding="utf-8")
    assert 'export PATH="$HOME/.local/bin:$PATH"' in bashrc_text

    bash_profile_text = bash_profile.read_text(encoding="utf-8")
    assert 'export PATH="$HOME/.local/bin:$PATH"' in bash_profile_text
    assert '. "$HOME/.bashrc"' in bash_profile_text
    assert result.bashrc_updated
    assert result.bash_profile_updated
    assert result.bash_profile_sources_bashrc_added


def test_ensure_opencode_host_path_setup_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    opencode_binary = home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin" / "opencode"
    _make_executable(opencode_binary)
    monkeypatch.setattr(opencode_setup.shutil, "which", lambda name: None)

    first = opencode_setup.ensure_opencode_host_path_setup(home)
    second = opencode_setup.ensure_opencode_host_path_setup(home)

    assert first.symlink_created or first.symlink_updated or first.symlink_already_correct
    assert not second.symlink_created
    assert not second.symlink_updated
    assert second.symlink_already_correct
    assert not second.bashrc_updated
    assert not second.bash_profile_updated
    assert not second.bash_profile_sources_bashrc_added


def test_ensure_opencode_host_path_setup_reports_blocked_symlink_path(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    opencode_binary = home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin" / "opencode"
    _make_executable(opencode_binary)
    blocked = home / ".local" / "bin" / "opencode"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("not a symlink\n", encoding="utf-8")
    blocked.chmod(blocked.stat().st_mode | os.X_OK)

    monkeypatch.setattr(opencode_setup.shutil, "which", lambda name: None)

    result = opencode_setup.ensure_opencode_host_path_setup(home)

    assert result.symlink_blocked
    assert not result.symlink_already_correct
