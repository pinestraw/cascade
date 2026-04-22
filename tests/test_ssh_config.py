from __future__ import annotations

from pathlib import Path

import pytest

from cascade.ssh_config import (
    ensure_github_known_host,
    sanitize_ssh_config_file,
    sanitize_ssh_config_text,
)


def test_sanitize_ssh_config_text_removes_usekeychain_case_insensitive() -> None:
    source = (
        "# host defaults\n"
        "Host github.com\n"
        "  HostName github.com\n"
        "  User git\n"
        "  IdentityFile ~/.ssh/id_ed25519\n"
        "  UseKeychain yes\n"
        "\n"
        "Host *\n"
        "  usekeychain no\n"
        "  AddKeysToAgent yes\n"
    )

    sanitized = sanitize_ssh_config_text(source)

    assert "\n  UseKeychain yes\n" not in sanitized
    assert "\n  usekeychain no\n" not in sanitized
    assert "# host defaults" in sanitized
    assert "Host github.com" in sanitized
    assert "IdentityFile ~/.ssh/id_ed25519" in sanitized
    assert "AddKeysToAgent yes" in sanitized
    assert "# Removed by Cascade Docker SSH sanitizer" in sanitized


def test_sanitize_ssh_config_file_writes_minimal_when_source_missing(tmp_path: Path) -> None:
    source = tmp_path / "missing-config"
    dest = tmp_path / ".cascade" / "ssh" / "config"

    sanitize_ssh_config_file(source=source, dest=dest)

    written = dest.read_text(encoding="utf-8")
    assert "Host github.com" in written
    assert "HostName github.com" in written
    assert "User git" in written
    assert "IdentitiesOnly yes" in written


def test_sanitize_ssh_config_file_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "source-config"
    source.write_text("Host github.com\n  UseKeychain yes\n", encoding="utf-8")
    dest = tmp_path / ".cascade" / "ssh" / "config"

    sanitize_ssh_config_file(source=source, dest=dest)

    assert source.read_text(encoding="utf-8") == "Host github.com\n  UseKeychain yes\n"
    assert "\n  UseKeychain yes\n" not in dest.read_text(encoding="utf-8")


def test_ensure_github_known_host_noop_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("github.com ssh-ed25519 AAAA...\n", encoding="utf-8")

    def _unexpected_call(*args, **kwargs):
        raise AssertionError("ssh-keyscan should not be called when github.com exists")

    monkeypatch.setattr("cascade.ssh_config.subprocess.run", _unexpected_call)

    ensure_github_known_host(known_hosts)

    assert known_hosts.read_text(encoding="utf-8") == "github.com ssh-ed25519 AAAA...\n"


def test_ensure_github_known_host_appends_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.com ssh-rsa AAAA...\n", encoding="utf-8")

    class _Completed:
        returncode = 0
        stdout = "github.com ssh-ed25519 BBBB...\n"
        stderr = ""

    monkeypatch.setattr("cascade.ssh_config.subprocess.run", lambda *args, **kwargs: _Completed())

    ensure_github_known_host(known_hosts)

    text = known_hosts.read_text(encoding="utf-8")
    assert "example.com ssh-rsa AAAA..." in text
    assert "github.com ssh-ed25519 BBBB..." in text


def test_ensure_github_known_host_raises_on_keyscan_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_hosts = tmp_path / "known_hosts"

    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "network error"

    monkeypatch.setattr("cascade.ssh_config.subprocess.run", lambda *args, **kwargs: _Completed())

    with pytest.raises(RuntimeError, match="Unable to add github.com"):
        ensure_github_known_host(known_hosts)
