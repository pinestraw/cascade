COMPOSE    := docker compose
SVC        := cascade
WRAPPER_TEMPLATE := scripts/cascade-docker-wrapper.sh
WRAPPER_BIN ?= $(HOME)/.local/bin/cascade

.PHONY: build setup docker-build rebuild shell test test-verbose test-fast doctor capabilities cascade env-check ssh-config ssh-check \
	install-wrapper uninstall-wrapper wrapper-check start check fix finish next status logs context estimate prepare opencode help

# ── Core Docker targets ────────────────────────────────────────────────────────

## build: prepare host prerequisites and build the Docker image
build: setup

## setup: prepare host prerequisites and build Docker image
setup: ssh-config install-wrapper docker-build

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

## install-wrapper: install host `cascade` wrapper that executes CLI in Docker
install-wrapper:
	mkdir -p "$(dir $(WRAPPER_BIN))"
	@if [ -e "$(WRAPPER_BIN)" ] && [ "$(FORCE)" != "1" ]; then \
		if ! grep -q 'docker compose run --rm cascade cascade' "$(WRAPPER_BIN)"; then \
			echo "Refusing to overwrite non-Cascade wrapper: $(WRAPPER_BIN)"; \
			echo "Run 'make install-wrapper FORCE=1' (or 'make build FORCE=1') to override."; \
			exit 1; \
		fi; \
	fi
	sed "s|__CASCADE_REPO__|$(CURDIR)|g" "$(WRAPPER_TEMPLATE)" > "$(WRAPPER_BIN)"
	chmod 755 "$(WRAPPER_BIN)"
	@echo "Installed wrapper: $(WRAPPER_BIN)"
	@echo "Wrapper executes: docker compose run --rm cascade cascade ..."
	@echo "If needed, add $(dir $(WRAPPER_BIN)) to PATH"

## uninstall-wrapper: remove installed host `cascade` wrapper
uninstall-wrapper:
	rm -f "$(WRAPPER_BIN)"
	@echo "Removed wrapper: $(WRAPPER_BIN)"

## wrapper-check: verify installed wrapper path and run `cascade --help`
wrapper-check:
	@test -x "$(WRAPPER_BIN)" || (echo "Wrapper missing: $(WRAPPER_BIN). Run 'make install-wrapper'." && exit 1)
	"$(WRAPPER_BIN)" --help >/dev/null
	@echo "Wrapper OK: $(WRAPPER_BIN)"

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
