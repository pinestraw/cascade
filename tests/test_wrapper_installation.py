from __future__ import annotations

import re
from pathlib import Path


def test_wrapper_template_contains_required_docker_exec() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "scripts" / "cascade-docker-wrapper.sh"
    text = wrapper.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert 'CASCADE_REPO="__CASCADE_REPO__"' in text
    assert 'exec docker compose run --rm cascade cascade "$@"' in text


def test_makefile_exposes_wrapper_targets() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "install-wrapper:" in text
    assert "uninstall-wrapper:" in text
    assert "wrapper-check:" in text
    assert "WRAPPER_BIN ?=" in text
    assert "WRAPPER_TEMPLATE := scripts/cascade-docker-wrapper.sh" in text


def test_makefile_build_setup_and_docker_build_flow() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "build: setup" in text
    assert "setup: ssh-config install-wrapper docker-build" in text
    assert "docker-build:" in text
    assert "$(COMPOSE) build $(SVC)" in text

    docker_build_block = re.search(r"(?m)^docker-build:\n(?P<body>(?:\t[^\n]*\n)+)", text)
    assert docker_build_block is not None
    body = docker_build_block.group("body")
    assert "$(COMPOSE) build $(SVC)" in body
    assert "ssh-config" not in body
    assert "install-wrapper" not in body


def test_makefile_install_wrapper_has_force_guard() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert 'if [ -e "$(WRAPPER_BIN)" ] && [ "$(FORCE)" != "1" ]' in text
    assert "Refusing to overwrite non-Cascade wrapper" in text
    assert "make install-wrapper FORCE=1" in text
