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


def test_host_wrapper_template_executes_host_native_cli() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "scripts" / "cascade-host-wrapper.sh"
    text = wrapper.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert 'CASCADE_REPO="__CASCADE_REPO__"' in text
    assert 'CASCADE_PYTHON="__CASCADE_PYTHON__"' in text
    assert 'CASCADE_WRAPPER_KIND="host"' in text
    assert 'exec "$CASCADE_PYTHON" -m cascade.cli "$@"' in text
    assert 'docker compose run --rm cascade cascade "$@"' not in text


def test_makefile_exposes_wrapper_targets() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "install-wrapper:" in text
    assert "install-wrapper-docker:" in text
    assert "uninstall-wrapper:" in text
    assert "uninstall-wrapper-docker:" in text
    assert "wrapper-check:" in text
    assert "wrapper-check-docker:" in text
    assert "wrapper-check-host:" in text
    assert "venv:" in text
    assert "install-venv:" in text
    assert "install:" in text
    assert "PYTHON ?=" in text
    assert "AUTO_HOST_PYTHON :=" in text
    assert "HOST_PYTHON :=" in text
    assert "WRAPPER_BIN ?=" in text
    assert "WRAPPER_DOCKER_BIN ?=" in text
    assert "WRAPPER_TEMPLATE := scripts/cascade-host-wrapper.sh" in text
    assert "WRAPPER_DOCKER_TEMPLATE := scripts/cascade-docker-wrapper.sh" in text


def test_makefile_build_setup_and_docker_build_flow() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "build: ssh-config docker-build" in text
    assert "setup: install" in text
    assert "install: install-host build install-wrapper-docker" in text
    assert "VENV_DIR ?= .venv" in text
    assert "VENV_PYTHON := $(VENV_DIR)/bin/python" in text
    assert "AUTO_HOST_PYTHON := $(if $(wildcard $(VENV_PYTHON)),$(VENV_PYTHON),python3)" in text
    assert "HOST_PYTHON := $(if $(strip $(PYTHON)),$(PYTHON),$(AUTO_HOST_PYTHON))" in text
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
    assert "Replacing stale Docker wrapper" in text
    assert "Refusing to overwrite non-Cascade wrapper" in text
    assert "make install-wrapper FORCE=1" in text


def test_makefile_install_wrapper_docker_has_force_guard() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert 'if [ -e "$(WRAPPER_DOCKER_BIN)" ] && [ "$(FORCE)" != "1" ]' in text
    assert "make install-wrapper-docker FORCE=1" in text


def test_makefile_install_host_prints_pep668_fallback_guidance() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "possible PEP 668 externally-managed environment" in text
    assert "Run: make install-venv PYTHON=/path/to/python3" in text


def test_makefile_install_venv_wires_wrapper_to_venv_python() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert "install-venv: venv" in text
    assert '$(VENV_PYTHON)" -m pip install -e ".[dev]"' in text
    assert '$(MAKE) install-wrapper PYTHON="$(VENV_PYTHON)"' in text


def test_makefile_host_targets_use_host_python_resolution() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    makefile = repo_root / "Makefile"
    text = makefile.read_text(encoding="utf-8")

    assert '"$(HOST_PYTHON)" -m venv "$(VENV_DIR)"' in text
    assert 'if ! "$(HOST_PYTHON)" -m pip install -e ".[dev]"; then' in text
    assert '$(MAKE) install-wrapper PYTHON="$(HOST_PYTHON)"' in text
    assert '"$(HOST_PYTHON)" -m cascade.cli opencode-setup' in text
    assert '"$(HOST_PYTHON)" -m cascade.cli doctor --project-file examples/jungle.yaml' in text
    assert "Using Python: $(HOST_PYTHON)" in text
