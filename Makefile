COMPOSE    := docker compose
SVC        := cascade
PYTHON ?=
VENV_DIR ?= .venv
VENV_PYTHON := $(VENV_DIR)/bin/python
AUTO_HOST_PYTHON := $(if $(wildcard $(VENV_PYTHON)),$(VENV_PYTHON),python3)
HOST_PYTHON := $(if $(strip $(PYTHON)),$(PYTHON),$(AUTO_HOST_PYTHON))
WRAPPER_TEMPLATE := scripts/cascade-host-wrapper.sh
WRAPPER_DOCKER_TEMPLATE := scripts/cascade-docker-wrapper.sh
WRAPPER_BIN ?= $(HOME)/.local/bin/cascade
WRAPPER_DOCKER_BIN ?= $(HOME)/.local/bin/cascade-docker

.PHONY: build setup docker-build rebuild shell test test-verbose test-fast test-integration test-smoke doctor capabilities cascade env-check ssh-config ssh-check \
	venv install-venv install install-wrapper uninstall-wrapper wrapper-check install-wrapper-docker uninstall-wrapper-docker wrapper-check-docker wrapper-check-host install-host doctor-host test-host \
	start check fix finish closeout next status logs context estimate prepare opencode help

# ── Core Docker targets ────────────────────────────────────────────────────────

## build: build Docker image only (host wrapper installation is separate)
build: ssh-config docker-build
	@echo "Docker image built. To install host-native CLI, run: make install-host"

## setup: backward-compatible alias for full local setup
setup: install

## install: recommended one-shot local setup
install: install-host build install-wrapper-docker
	@echo "Setup complete: 'cascade' is host-native and 'cascade-docker' is Docker-backed."

## venv: create repo-local virtualenv and upgrade packaging tools
venv:
	@echo "Using Python: $(HOST_PYTHON)"
	"$(HOST_PYTHON)" -m venv "$(VENV_DIR)"
	"$(VENV_PYTHON)" -m pip install --upgrade pip setuptools wheel
	@echo "Virtualenv ready: $(CURDIR)/$(VENV_DIR)"
	@echo "Python: $(VENV_PYTHON)"

## docker-build: build only the Docker image (no host setup)
docker-build:
	$(COMPOSE) build $(SVC)

## rebuild: force a clean rebuild without cache (use after .env changes)
rebuild:
	$(COMPOSE) build --no-cache $(SVC)

## ssh-config: generate Docker-safe SSH config at ~/.cascade/ssh/config
ssh-config:
	python3 scripts/prepare_docker_ssh.py

## ssh-check: validate Docker SSH parsing/auth; optional fetch with REPO and BRANCH
ssh-check:
	$(COMPOSE) run --rm \
		-e REPO="$(REPO)" \
		-e BRANCH="$(or $(BRANCH),staging)" \
		$(SVC) bash -lc 'set -e; test -f /root/.ssh/config; ssh -G github.com >/dev/null; echo "SSH config parse: ok"; ssh -T git@github.com || true; if [ -n "$$REPO" ]; then echo "Fetch check: $$REPO $$BRANCH"; cd "$$REPO" && git fetch origin "$$BRANCH"; fi'

## install-wrapper: install host-native `cascade` wrapper
install-wrapper:
	mkdir -p "$(dir $(WRAPPER_BIN))"
	@if [ -e "$(WRAPPER_BIN)" ] && [ "$(FORCE)" != "1" ]; then \
		if grep -q 'docker compose run --rm cascade cascade' "$(WRAPPER_BIN)"; then \
			echo "Replacing stale Docker wrapper at $(WRAPPER_BIN) with host-native wrapper."; \
		elif grep -q 'CASCADE_WRAPPER_KIND="host"' "$(WRAPPER_BIN)"; then \
			: ; \
		else \
			echo "Refusing to overwrite non-Cascade wrapper: $(WRAPPER_BIN)"; \
			echo "Run 'make install-wrapper FORCE=1' to override."; \
			exit 1; \
		fi; \
	fi
	sed \
		-e "s|__CASCADE_REPO__|$(CURDIR)|g" \
		-e "s|__CASCADE_PYTHON__|$(HOST_PYTHON)|g" \
		"$(WRAPPER_TEMPLATE)" > "$(WRAPPER_BIN)"
	chmod 755 "$(WRAPPER_BIN)"
	@echo "Installed wrapper: $(WRAPPER_BIN)"
	@echo "Wrapper mode: host-native"
	@echo "Python: $(HOST_PYTHON)"
	@echo "Repo: $(CURDIR)"
	@echo "Test command: cascade --help"
	@echo "If needed, add $(dir $(WRAPPER_BIN)) to PATH"

## install-wrapper-docker: install explicit Docker wrapper as `cascade-docker`
install-wrapper-docker:
	mkdir -p "$(dir $(WRAPPER_DOCKER_BIN))"
	@if [ -e "$(WRAPPER_DOCKER_BIN)" ] && [ "$(FORCE)" != "1" ]; then \
		if ! grep -q 'docker compose run --rm cascade cascade' "$(WRAPPER_DOCKER_BIN)"; then \
			echo "Refusing to overwrite non-Cascade wrapper: $(WRAPPER_DOCKER_BIN)"; \
			echo "Run 'make install-wrapper-docker FORCE=1' to override."; \
			exit 1; \
		fi; \
	fi
	sed "s|__CASCADE_REPO__|$(CURDIR)|g" "$(WRAPPER_DOCKER_TEMPLATE)" > "$(WRAPPER_DOCKER_BIN)"
	chmod 755 "$(WRAPPER_DOCKER_BIN)"
	@echo "Installed Docker wrapper: $(WRAPPER_DOCKER_BIN)"

## uninstall-wrapper: remove installed host `cascade` wrapper
uninstall-wrapper:
	rm -f "$(WRAPPER_BIN)"
	@echo "Removed wrapper: $(WRAPPER_BIN)"

## uninstall-wrapper-docker: remove installed `cascade-docker` wrapper
uninstall-wrapper-docker:
	rm -f "$(WRAPPER_DOCKER_BIN)"
	@echo "Removed wrapper: $(WRAPPER_DOCKER_BIN)"

## wrapper-check: verify `cascade` host wrapper path and run `cascade --help`
wrapper-check:
	@test -x "$(WRAPPER_BIN)" || (echo "Wrapper missing: $(WRAPPER_BIN). Run 'make install-host'." && exit 1)
	@resolved="$$(command -v cascade || true)"; \
	if [ -z "$$resolved" ]; then \
		echo "'cascade' is not on PATH. Add $(dir $(WRAPPER_BIN)) to PATH."; \
		exit 1; \
	fi; \
	if ! [ -x "$$resolved" ]; then \
		echo "'cascade' on PATH is not executable: $$resolved"; \
		exit 1; \
	fi; \
	if grep -q 'docker compose run --rm cascade cascade' "$$resolved"; then \
		echo "Error: PATH 'cascade' command points to a Docker wrapper: $$resolved"; \
		echo "Run 'make install-host' and ensure $(dir $(WRAPPER_BIN)) is before stale locations in PATH."; \
		exit 1; \
	fi; \
	if ! grep -q 'CASCADE_WRAPPER_KIND="host"' "$$resolved"; then \
		echo "Error: PATH 'cascade' command is not a recognized Cascade host wrapper: $$resolved"; \
		echo "Run 'make install-host' (or FORCE=1 if intentional)."; \
		exit 1; \
	fi
	@if grep -q 'docker compose run --rm cascade cascade' "$(WRAPPER_BIN)"; then \
		echo "Error: $(WRAPPER_BIN) is still a Docker wrapper."; \
		echo "Run 'make install-host' to install the host-native wrapper."; \
		exit 1; \
	fi
	@if ! grep -q 'CASCADE_WRAPPER_KIND="host"' "$(WRAPPER_BIN)"; then \
		echo "Error: $(WRAPPER_BIN) is not a recognized Cascade host wrapper."; \
		echo "Run 'make install-host' (or FORCE=1 if intentional)."; \
		exit 1; \
	fi
	@echo "Using Python: $(HOST_PYTHON)"
	cascade --help >/dev/null
	@echo "Host wrapper OK: $(WRAPPER_BIN)"

## wrapper-check-docker: verify `cascade-docker` wrapper path and run `cascade-docker --help`
wrapper-check-docker:
	@test -x "$(WRAPPER_DOCKER_BIN)" || (echo "Wrapper missing: $(WRAPPER_DOCKER_BIN). Run 'make install-wrapper-docker'." && exit 1)
	@resolved="$$(command -v cascade-docker || true)"; \
	if [ -z "$$resolved" ]; then \
		echo "'cascade-docker' is not on PATH. Add $(dir $(WRAPPER_DOCKER_BIN)) to PATH."; \
		exit 1; \
	fi; \
	if ! grep -q 'docker compose run --rm cascade cascade' "$$resolved"; then \
		echo "Error: PATH 'cascade-docker' command is not a recognized Docker wrapper: $$resolved"; \
		exit 1; \
	fi
	cascade-docker --help >/dev/null
	@echo "Docker wrapper OK: $(WRAPPER_DOCKER_BIN)"

## wrapper-check-host: backward-compatible alias for host wrapper check
wrapper-check-host:
	@$(MAKE) wrapper-check

## install-host: install host-native Cascade dependencies and wrapper
install-host:
	@set -e; \
	echo "Using Python: $(HOST_PYTHON)"; \
	if ! "$(HOST_PYTHON)" -m pip install -e ".[dev]"; then \
		echo ""; \
		echo "Host install failed (possible PEP 668 externally-managed environment)."; \
		echo "Run: make install-venv PYTHON=/path/to/python3"; \
		exit 1; \
	fi
	$(MAKE) install-wrapper PYTHON="$(HOST_PYTHON)"
	"$(HOST_PYTHON)" -m cascade.cli opencode-setup
	@echo "Installed host-native Cascade"
	@echo "Installed wrapper path: $(WRAPPER_BIN)"
	@echo "Python used: $(HOST_PYTHON)"
	@echo "Repo path: $(CURDIR)"
	@echo "Test command: cascade --help"

## install-venv: install host-native Cascade into repo-local virtualenv and wire wrapper to venv python
install-venv: venv
	@echo "Using Python: $(VENV_PYTHON)"
	"$(VENV_PYTHON)" -m pip install -e ".[dev]"
	$(MAKE) install-wrapper PYTHON="$(VENV_PYTHON)"
	@echo "Installed host-native Cascade into virtualenv"
	@echo "Installed wrapper path: $(WRAPPER_BIN)"
	@echo "Python used: $(VENV_PYTHON)"
	@echo "Repo path: $(CURDIR)"
	@echo "Test command: cascade --help"

## doctor-host: run cascade doctor directly on the host
doctor-host:
	@echo "Using Python: $(HOST_PYTHON)"
	"$(HOST_PYTHON)" -m cascade.cli opencode-setup
	"$(HOST_PYTHON)" -m cascade.cli doctor --project-file examples/jungle.yaml

## test-host: run tests directly on the host
test-host:
	$(PYTHON) -m pytest tests/ -q

## shell: open a bash shell inside the cascade container
shell:
	$(COMPOSE) run --rm $(SVC) bash

## test: run all tests inside Docker (quiet)
test:
	$(COMPOSE) run --rm $(SVC) python -m pytest tests/ -q

## test-verbose: run all tests with full output
test-verbose:
	$(COMPOSE) run --rm $(SVC) python -m pytest tests/ -v

## test-fast: run tests, stop at first failure
test-fast:
	$(COMPOSE) run --rm $(SVC) python -m pytest tests/ -x -q

## test-integration: run medium integration tier
test-integration:
	$(COMPOSE) run --rm $(SVC) python -m pytest -m integration -q

## test-smoke: run smoke scripts (manual/nightly)
test-smoke:
	@for script in tests/smoke/*.sh; do \
		echo "Running $$script"; \
		"$$script"; \
	done

## doctor: run cascade doctor with examples/jungle.yaml
doctor:
	$(COMPOSE) run --rm $(SVC) cascade doctor --project-file examples/jungle.yaml

## capabilities: show cascade capabilities table
capabilities:
	$(COMPOSE) run --rm $(SVC) cascade capabilities

## env-check: verify Docker sees GitHub and model env vars without printing values
env-check:
	$(COMPOSE) run --rm $(SVC) bash -lc 'echo GH_TOKEN=$${GH_TOKEN:+set}; echo OPENROUTER_API_KEY=$${OPENROUTER_API_KEY:+set}; gh issue view 44 --repo pinestraw/jungle --json number,title'

# ── Generic cascade wrapper ────────────────────────────────────────────────────
# Usage: make cascade ARGS="<command> [options]"
# Example: make cascade ARGS="status --project jungle"

cascade:
ifndef ARGS
	@echo "Usage: make cascade ARGS=\"<cascade command and args>\""
	@echo "Examples:"
	@echo "  make cascade ARGS=\"capabilities\""
	@echo "  make cascade ARGS=\"status --project jungle\""
	@echo "  make cascade ARGS=\"doctor --project-file examples/jungle.yaml\""
	@exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade $(ARGS)

# ── Mandate workflow targets ───────────────────────────────────────────────────
# These are thin wrappers so you can drive Cascade entirely from Make without
# memorising the full CLI syntax.

## start: claim an issue and launch the agent
##   Required: ISSUE=<n> AGENT=<name> PROJECT_FILE=<yaml>
##   Optional: PROFILE=<name>
start:
ifndef ISSUE
	@echo "Usage: make start ISSUE=<n> AGENT=<name> PROJECT_FILE=<yaml> [PROFILE=<name>]"; exit 1
endif
ifndef AGENT
	@echo "Usage: make start ISSUE=<n> AGENT=<name> PROJECT_FILE=<yaml> [PROFILE=<name>]"; exit 1
endif
ifndef PROJECT_FILE
	@echo "Usage: make start ISSUE=<n> AGENT=<name> PROJECT_FILE=<yaml> [PROFILE=<name>]"; exit 1
endif
ifdef PROFILE
	$(COMPOSE) run --rm $(SVC) cascade start $(ISSUE) --agent $(AGENT) --project-file $(PROJECT_FILE) --profile $(PROFILE)
else
	$(COMPOSE) run --rm $(SVC) cascade start $(ISSUE) --agent $(AGENT) --project-file $(PROJECT_FILE)
endif

## check: run preflight for an agent
##   Required: AGENT=<name> PROJECT=<name>
check:
ifndef AGENT
	@echo "Usage: make check AGENT=<name> PROJECT=<name>"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make check AGENT=<name> PROJECT=<name>"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade check $(AGENT) --project $(PROJECT)

## fix: run fix cycle for an agent
##   Required: AGENT=<name> PROJECT=<name>
##   Optional: PROFILE=<name>
fix:
ifndef AGENT
	@echo "Usage: make fix AGENT=<name> PROJECT=<name> [PROFILE=<name>]"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make fix AGENT=<name> PROJECT=<name> [PROFILE=<name>]"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade fix $(AGENT) --project $(PROJECT) --profile $(or $(PROFILE),debugger)

## finish: close out a mandate (dry-run by default; set YES=1 to confirm)
##   Required: AGENT=<name> PROJECT=<name>
finish:
ifndef AGENT
	@echo "Usage: make finish AGENT=<name> PROJECT=<name> [YES=1]"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make finish AGENT=<name> PROJECT=<name> [YES=1]"; exit 1
endif
ifeq ($(YES),1)
	$(COMPOSE) run --rm $(SVC) cascade finish $(AGENT) --project $(PROJECT) --yes
else
	$(COMPOSE) run --rm $(SVC) cascade finish $(AGENT) --project $(PROJECT)
endif

## closeout: execute mandate-done closeout for an agent (requires YES=1)
##   Required: AGENT=<name> PROJECT=<name> YES=1
closeout:
ifndef AGENT
	@echo "Usage: make closeout AGENT=<name> PROJECT=<name> YES=1"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make closeout AGENT=<name> PROJECT=<name> YES=1"; exit 1
endif
ifneq ($(YES),1)
	@echo "Closeout is destructive. Re-run with YES=1."; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade closeout $(AGENT) --project $(PROJECT) --yes

## next: recommend the next high-level command for an agent
##   Required: AGENT=<name> PROJECT=<name>
next:
ifndef AGENT
	@echo "Usage: make next AGENT=<name> PROJECT=<name>"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make next AGENT=<name> PROJECT=<name>"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade next $(AGENT) --project $(PROJECT)

## status: show mandate status for a project
##   Required: PROJECT=<name>
status:
ifndef PROJECT
	@echo "Usage: make status PROJECT=<name>"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade status --project $(PROJECT)

## logs: show run logs for an agent
##   Required: AGENT=<name> PROJECT=<name>
##   Optional: KIND=preflight|prompt|mandate (default: preflight)
logs:
ifndef AGENT
	@echo "Usage: make logs AGENT=<name> PROJECT=<name> [KIND=preflight|prompt|mandate]"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make logs AGENT=<name> PROJECT=<name> [KIND=preflight|prompt|mandate]"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade logs $(AGENT) --project $(PROJECT) --kind $(or $(KIND),preflight)

## context: build a context pack for an agent task
##   Required: AGENT=<name> PROJECT=<name> TASK=<type>
context:
ifndef AGENT
	@echo "Usage: make context AGENT=<name> PROJECT=<name> TASK=<type>"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make context AGENT=<name> PROJECT=<name> TASK=<type>"; exit 1
endif
ifndef TASK
	@echo "Usage: make context AGENT=<name> PROJECT=<name> TASK=<type>"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade context-pack $(AGENT) --project $(PROJECT) --task $(TASK)

## estimate: estimate model call cost for an agent task
##   Required: AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name>
##   Optional: OUT=<tokens> (expected output tokens; default determined by cascade)
estimate:
ifndef AGENT
	@echo "Usage: make estimate AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name> [OUT=<tokens>]"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make estimate AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name> [OUT=<tokens>]"; exit 1
endif
ifndef TASK
	@echo "Usage: make estimate AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name> [OUT=<tokens>]"; exit 1
endif
ifndef PROFILE
	@echo "Usage: make estimate AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name> [OUT=<tokens>]"; exit 1
endif
ifdef OUT
	$(COMPOSE) run --rm $(SVC) cascade estimate-cost $(AGENT) --project $(PROJECT) --task $(TASK) --profile $(PROFILE) --expected-output-tokens $(OUT)
else
	$(COMPOSE) run --rm $(SVC) cascade estimate-cost $(AGENT) --project $(PROJECT) --task $(TASK) --profile $(PROFILE)
endif

## prepare: prepare a model call context pack and estimate
##   Required: AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name>
prepare:
ifndef AGENT
	@echo "Usage: make prepare AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name>"; exit 1
endif
ifndef PROJECT
	@echo "Usage: make prepare AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name>"; exit 1
endif
ifndef TASK
	@echo "Usage: make prepare AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name>"; exit 1
endif
ifndef PROFILE
	@echo "Usage: make prepare AGENT=<name> PROJECT=<name> TASK=<type> PROFILE=<name>"; exit 1
endif
	$(COMPOSE) run --rm $(SVC) cascade prepare-model-call $(AGENT) --project $(PROJECT) --task $(TASK) --profile $(PROFILE)

# ── OpenCode convenience target ────────────────────────────────────────────────
# Run OpenCode interactively inside the cascade container.
#
# Preferred usage (workspace-relative path, no leading ../):
#   make opencode PATH=jungle-worktrees/oc1-slug MODEL=openrouter/z-ai/glm-4.7
#
# Legacy usage (still supported for backward compat):
#   make opencode WORKTREE=../jungle-worktrees/oc1-slug MODEL=openrouter/z-ai/glm-4.7
#
# Without PATH or WORKTREE, prints usage and opens a plain bash shell.

opencode:
ifdef PATH
	$(COMPOSE) run --rm --workdir /workspace/$(PATH) \
		$(SVC) opencode . $(if $(MODEL),--model $(MODEL),)
else ifdef WORKTREE
	$(COMPOSE) run --rm --workdir /workspace/$(patsubst ../%,%,$(WORKTREE)) \
		$(SVC) opencode . $(if $(MODEL),--model $(MODEL),)
else
	@echo "Usage: make opencode PATH=<workspace-relative-path> [MODEL=<model-id>]"
	@echo "Example: make opencode PATH=jungle-worktrees/oc1-daily-digest MODEL=openrouter/z-ai/glm-4.7"
	@echo ""
	@echo "Opening shell instead — run 'opencode <path>' manually."
	$(COMPOSE) run --rm $(SVC) bash
endif

# ── Help ──────────────────────────────────────────────────────────────────────

## help: show this help
help:
	@grep -E '^## ' Makefile | sed 's/^## //'
