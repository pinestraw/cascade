import errno
import os
import subprocess

_REMOVED_OPTIONS = {"usekeychain"}

_MINIMAL_DOCKER_SSH_CONFIG = """Host github.com
  HostName github.com
  User git
  IdentitiesOnly yes
"""


def _ensure_dir(path):
    path_str = str(path)
    try:
        os.makedirs(path_str)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise


def _path_exists(path):
    return os.path.exists(str(path))


def _read_text(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _write_text(path, text):
    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write(text)


def _append_text(path, text):
    with open(str(path), "a", encoding="utf-8") as handle:
        handle.write(text)


def _option_key(line):
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    token = stripped.split(maxsplit=1)[0]
    key = token.split("=", maxsplit=1)[0].strip()
    return key.lower() if key else None


def sanitize_ssh_config_text(text):
    """Return Docker-safe SSH config text.

    Removes macOS-only options that Linux OpenSSH does not understand while
    preserving comments, spacing, and unchanged line indentation.
    """

    sanitized_lines = []
    for line in text.splitlines():
        key = _option_key(line)
        if key in _REMOVED_OPTIONS:
            leading = line[: len(line) - len(line.lstrip())]
            sanitized_lines.append("{}# Removed by Cascade Docker SSH sanitizer: {}".format(leading, line.lstrip()))
            continue
        sanitized_lines.append(line)

    sanitized = "\n".join(sanitized_lines)
    if text.endswith("\n"):
        sanitized += "\n"
    return sanitized


def sanitize_ssh_config_file(source, dest):
    """Write a sanitized SSH config for Docker usage.

    The source file remains unchanged. If source is missing, a minimal GitHub
    config is written.
    """

    dest_str = str(dest)
    _ensure_dir(os.path.dirname(dest_str))

    if _path_exists(source):
        source_text = _read_text(source)
        output = sanitize_ssh_config_text(source_text)
    else:
        output = _MINIMAL_DOCKER_SSH_CONFIG

    _write_text(dest, output)


def ensure_github_known_host(known_hosts_path):
    """Ensure github.com host keys exist in known_hosts without duplication."""

    known_hosts_str = str(known_hosts_path)
    _ensure_dir(os.path.dirname(known_hosts_str))
    existing = _read_text(known_hosts_path) if _path_exists(known_hosts_path) else ""
    if "github.com" in existing:
        return

    result = subprocess.run(
        ["ssh-keyscan", "github.com"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        error_output = (result.stderr or result.stdout or "").strip() or "ssh-keyscan returned no output"
        raise RuntimeError("Unable to add github.com to known_hosts: {}".format(error_output))

    append_prefix = "" if not existing or existing.endswith("\n") else "\n"
    _append_text(known_hosts_path, "{}{}".format(append_prefix, result.stdout))
