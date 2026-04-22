COMPOSE    := docker compose
SVC        := cascade

.PHONY: build shell test test-verbose test-fast doctor capabilities cascade \
        start check fix finish status logs context estimate prepare opencode help

# ── Core Docker targets ────────────────────────────────────────────────────────

## build: build the Docker image
build:
	$(COMPOSE) build $(SVC)

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
ifdef PROFILE
	$(COMPOSE) run --rm $(SVC) cascade fix $(AGENT) --project $(PROJECT) --profile $(PROFILE)
else
	$(COMPOSE) run --rm $(SVC) cascade fix $(AGENT) --project $(PROJECT)
endif

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
	$(COMPOSE) run --rm $(SVC) cascade finish $(AGENT) --project $(PROJECT) --dry-run
endif

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
# Usage (with target worktree and model):
#   make opencode WORKTREE=../jungle-worktrees/oc1-slug MODEL=openrouter/z-ai/glm-4.7
#
# Without WORKTREE, opens a plain bash shell for manual use.

opencode:
ifdef WORKTREE
	$(COMPOSE) run --rm --workdir /workspace/$(patsubst ../%,%,$(WORKTREE)) \
		$(SVC) opencode . $(if $(MODEL),--model $(MODEL),)
else
	@echo "Usage: make opencode WORKTREE=<relative-path-to-worktree> [MODEL=<model-id>]"
	@echo "Example: make opencode WORKTREE=../jungle-worktrees/oc1-daily-digest MODEL=openrouter/z-ai/glm-4.7"
	@echo ""
	@echo "Opening shell instead — run 'opencode <path>' manually."
	$(COMPOSE) run --rm $(SVC) bash
endif

# ── Help ──────────────────────────────────────────────────────────────────────

## help: show this help
help:
	@grep -E '^## ' Makefile | sed 's/^## //'
