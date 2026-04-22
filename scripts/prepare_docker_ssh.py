#!/usr/bin/env python3
import errno
import os
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cascade.ssh_config import ensure_github_known_host, sanitize_ssh_config_file


def _chmod(path, mode):
    os.chmod(str(path), mode)


def _ensure_dir(path):
    path_str = str(path)
    try:
        os.makedirs(path_str)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise


def main():
    home = os.path.expanduser("~")
    source = os.path.join(home, ".ssh", "config")
    known_hosts = os.path.join(home, ".ssh", "known_hosts")
    cascade_root = os.path.join(home, ".cascade")
    docker_ssh_dir = os.path.join(cascade_root, "ssh")
    dest = os.path.join(docker_ssh_dir, "config")

    _ensure_dir(cascade_root)
    _ensure_dir(docker_ssh_dir)

    sanitize_ssh_config_file(source=source, dest=dest)
    ensure_github_known_host(known_hosts)

    _chmod(cascade_root, stat.S_IRWXU)
    _chmod(docker_ssh_dir, stat.S_IRWXU)
    _chmod(dest, stat.S_IRUSR | stat.S_IWUSR)

    generated_from = source if os.path.exists(source) else "(minimal fallback)"
    print("SSH source      : {}".format(generated_from))
    print("SSH destination : {}".format(dest))
    print("Generated Docker-safe SSH config for Cascade.")
    print("Docker Compose mounts this file over /root/.ssh/config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
