# Cascade

Cascade is a small, reproducible local mandate runner for multi-agent development.

It is the controller, not the target repo. Project-specific behavior lives in YAML so Cascade can orchestrate different repositories with different worktree and validation commands.

This MVP does four practical things well:

- load a project config
- fetch a GitHub issue through the local `gh` CLI
- create and track a per-agent worktree run locally
- launch `opencode` in the assigned worktree with a generated prompt

The conversational layer keeps durable context outside OpenCode so sessions are recoverable even if OpenCode continuation is imperfect.

## Model-minimal architecture

Cascade intentionally keeps most steps deterministic. Model calls are reserved for planning, implementing, diagnosing, fixing, and reviewing workflows.

| Command | Model required? | OpenCode required? | Notes |
|---|---:|---:|---|
| `doctor` | no | no | deterministic prerequisites and config checks |
| `claim` | no | no | deterministic issue/worktree/state setup |
| `status` | no | no | local state dashboard |
| `show-prompt` | no | no | prompt file output |
| `mark` | no | no | local lifecycle state update |
| `note` | no | no | deterministic clarification storage |
| `context` | no | no | deterministic context generation |
| `diff` | no | no | deterministic git summary |
| `logs` | no | no | run artifact output |
| `preflight` | no | no | deterministic configured validation run |
| `capabilities` | no | no | command category and capability matrix |
| `run-agent` | yes | yes | OpenCode interactive session |
| `chat` | yes | yes | OpenCode interactive session with optional mode |
| `ask` | yes | yes | OpenCode model-backed follow-up question |
| `summarize` | yes | yes | OpenCode model-backed summary refresh |
| `continue` | yes | yes | OpenCode continuation session |

## MVP workflow

1. Claim a GitHub issue for an agent with `cascade claim`.
2. Cascade fetches the issue body, derives a slug, runs the configured worktree command, and writes local state.
3. Start the agent session with `cascade run-agent`.
4. Cascade launches OpenCode in the assigned worktree and loads the generated prompt automatically when available.
5. Track progress with `cascade status` and `cascade mark`.

## Requirements

- Python 3.11+
- GitHub CLI (`gh`) authenticated with access to the target repo
- OpenCode installed and available on `PATH`
- target repos cloned next to `cascade` if you use the example config

On macOS, install a suitable Python with `brew install python@3.11` or `pyenv install 3.11`.
Your active dev shell must be running Python 3.11+ before creating the virtualenv.

## Install

```bash
cp .env.example .env   # fill in API keys
make build             # prepare SSH + wrapper, then build the Docker image
```

`make build` now performs standard host/Docker setup:

- prepares Docker-safe SSH config (`make ssh-config`)
- installs/updates the host `cascade` wrapper (`make install-wrapper`)
- builds the Docker image (`make docker-build`)

If you need image build only (no host setup changes), use:

```bash
make docker-build
```

For running Cascade locally (outside Docker), you still need a 3.11+ Python and can install with:

```bash
pip install -e .
```

## Running with Docker Compose

Docker is the preferred reproducible workflow.  The container includes Python,
Node.js, OpenCode (`opencode-ai`), the GitHub CLI (`gh`), the Docker CLI with
the Compose plugin, and Cascade itself.

Cascade uses the host Docker daemon through the mounted host socket at
`/var/run/docker.sock`. It does not run privileged Docker-in-Docker and does
not start a nested Docker daemon.

## Host `cascade` wrapper (Docker-backed)

You can install a host-side `cascade` wrapper so commands run in Docker Compose
without typing `make cascade ARGS=...`.

Install:

```bash
make install-wrapper
make wrapper-check
```

Then use from your host shell:

```bash
cascade status --project jungle
cascade claim --project-file examples/jungle.yaml --issue 44 --agent a1 --engine opencode --model openrouter/z-ai/glm-4.7
cascade run-agent a1 --project jungle
```

Notes:

- The wrapper is installed by default at `~/.local/bin/cascade`.
- It runs `docker compose run --rm cascade cascade "$@"` from the Cascade repo root.
- Interactive commands (`run-agent`, `chat`) preserve TTY behavior.
- Docker-backed target repo commands run against the host Docker daemon through `/var/run/docker.sock`.
- Customize install path with `make install-wrapper WRAPPER_BIN=/custom/path/cascade`.
- If an existing non-Cascade wrapper is present, install/build fails safely.
- Override that guard with `make install-wrapper FORCE=1` or `make build FORCE=1`.
- Verify installation with `make wrapper-check`.

## Docker SSH setup for private repos

Cascade often runs inside Docker while operating on sibling private repositories
mounted from the host workspace. Host SSH keys are mounted read-only, but macOS
SSH configs often include `UseKeychain yes`, which Linux OpenSSH rejects.

Cascade provides a Docker-safe SSH config flow:

- source: `~/.ssh/config`
- generated destination: `~/.cascade/ssh/config`
- container mount override: `/root/.ssh/config`

Run this before `make shell` if your target repos use SSH remotes:

```bash
make ssh-config
make build
make shell
ssh -T git@github.com
```

Validation helpers:

```bash
make ssh-check
make ssh-check REPO=/workspace/jungle BRANCH=staging
```

Notes:

- `make ssh-config` never copies private keys into the repo or Docker image.
- It sanitizes only the Docker-specific config file at `~/.cascade/ssh/config`.
- Keep `~/.ssh` as your host source of truth.

## Dedicated workspace layout

Cascade is designed to run from a dedicated workspace folder that contains only
the repos for a single project family.  This keeps Docker mounts scoped and
prevents accidental edits to unrelated sibling repos.

### Recommended layout

```text
instica-workspace/
  cascade/              ← run Cascade from here
  jungle/
  jungle-worktrees/
  jungle-secrets/
  jungle-infrastructure/
  instica.code-workspace
```

### Why a dedicated workspace?

- `docker-compose.yml` mounts `..:/workspace`, so `..` must be the workspace
  root, not a broad folder like `~/Documents` or `~/github-projects`.
- `examples/jungle.yaml` sets `workspace_root: ".."` so all project paths
  resolve relative to the workspace root rather than using `../jungle` etc.
- Cascade only operates on paths explicitly declared in the project YAML.
  Sibling repos that are not declared will never be touched.

### Project YAML: new-style paths

```yaml
paths:
  workspace_root: ".."     # resolved relative to CWD (cascade/)
  repo_root: "jungle"      # resolved relative to workspace_root
  worktree_root: "jungle-worktrees"
  secrets_root: "jungle-secrets"

related_repos:
  infrastructure: "jungle-infrastructure"
```

Expected resolved paths when running from `instica-workspace/cascade`:

```
workspace_root  => instica-workspace
repo_root       => instica-workspace/jungle
worktree_root   => instica-workspace/jungle-worktrees
secrets_root    => instica-workspace/jungle-secrets
related_repos.infrastructure => instica-workspace/jungle-infrastructure
```

Inside Docker the same paths resolve to `/workspace`, `/workspace/jungle`, etc.

### Workspace safety rules enforced by Cascade

- `repo_root`, `worktree_root`, `secrets_root`, and all `related_repos` must
  resolve inside `workspace_root` — escape paths (`../../`) are rejected.
- Model prompts include: *"Only operate inside the assigned worktree and
  explicitly declared project paths. Do not inspect or edit unrelated sibling
  repositories in the workspace."*
- A broad `workspace_root` (e.g. `github-projects`, `Documents`, home
  directory) triggers a `warn` in `cascade doctor`.

### VS Code workspace file

Include only repos that are part of the active project:

```json
{
  "folders": [
    { "path": "cascade" },
    { "path": "jungle" },
    { "path": "jungle-worktrees" },
    { "path": "jungle-secrets" },
    { "path": "jungle-infrastructure" }
  ]
}
```

### First-time setup

```bash
cd ~/Documents/instica-workspace/cascade
cp .env.example .env
# Add OPENROUTER_API_KEY, GH_TOKEN, and GITHUB_TOKEN to .env

make build
make shell        # opens bash inside the container

# Inside the shell:
pwd               # /workspace/cascade
ls /workspace     # cascade  jungle  jungle-worktrees  jungle-secrets  jungle-infrastructure
cascade doctor --project-file examples/jungle.yaml
opencode --version
```

### API keys

**Option A** (recommended): set keys in `.env` before `make build`.

```ini
OPENROUTER_API_KEY=sk-or-...
GH_TOKEN=ghp_...
GITHUB_TOKEN=ghp_...
```

Set both `GH_TOKEN` and `GITHUB_TOKEN` in `.env` for GitHub CLI compatibility inside Docker.

**Option B**: run `make shell`, then inside the container run `opencode` and
use `/connect` to authenticate interactively.  Auth persists in the
`opencode-data` Docker volume.

**GitHub CLI**: either set both `GH_TOKEN` and `GITHUB_TOKEN` in `.env`, or run `gh auth login`
inside `make shell`.

### Run tests

```bash
make test           # quiet
make test-verbose   # full -v output
make test-fast      # stop at first failure
```

### Deterministic checks (no model needed)

```bash
make doctor
make capabilities
make env-check
```

### Generic command wrapper

```bash
make cascade ARGS="capabilities"
make cascade ARGS="status --project jungle"
make cascade ARGS="doctor --project-file examples/jungle.yaml"
```

### Mandate happy path

```bash
make start ISSUE=45 AGENT=oc1 PROJECT_FILE=examples/jungle.yaml PROFILE=executor
make check AGENT=oc1 PROJECT=jungle
make fix   AGENT=oc1 PROJECT=jungle PROFILE=debugger
make next  AGENT=oc1 PROJECT=jungle
make check AGENT=oc1 PROJECT=jungle
make finish AGENT=oc1 PROJECT=jungle          # dry-run (safe preview)
make finish AGENT=oc1 PROJECT=jungle YES=1    # mark closeout_ready
```

### Other workflow targets

```bash
make status  PROJECT=jungle
make next    AGENT=oc1 PROJECT=jungle
make logs    AGENT=oc1 PROJECT=jungle KIND=preflight
make context AGENT=oc1 PROJECT=jungle TASK=implement
make estimate AGENT=oc1 PROJECT=jungle TASK=implement PROFILE=executor OUT=30000
make prepare  AGENT=oc1 PROJECT=jungle TASK=implement PROFILE=executor
```

### Run OpenCode interactively in a worktree

```bash
# Preferred: workspace-relative PATH (no leading ../)
make opencode PATH=jungle-worktrees/oc1-daily-digest-email MODEL=openrouter/z-ai/glm-4.7

# Legacy: WORKTREE= still works for backward compat
make opencode WORKTREE=../jungle-worktrees/oc1-daily-digest-email MODEL=openrouter/z-ai/glm-4.7
```

If neither `PATH` nor `WORKTREE` is set, a plain bash shell opens so you can
run `opencode <path>` manually.

### Limitations

- `make finish YES=1` marks the agent `closeout_ready` after deterministic safety checks pass.
- `~/.ssh` and `~/.gitconfig` are mounted read-only; adjust the volume entries
  in `docker-compose.yml` if your paths differ.
- GitHub CLI auth persists across containers via the `gh-config` Docker volume
  (`/root/.config/gh`).  Run `gh auth login` inside `make shell` once.
- OpenCode auth persists via the `opencode-data` Docker volume.  Use `/connect`
  inside `make shell` to authenticate interactively.

## Example usage

```bash
cascade claim --project-file examples/jungle.yaml --issue 45 --agent oc1 --model openrouter/z-ai/glm-4.7-flash
cascade doctor --project-file examples/jungle.yaml
cascade status --project jungle
cascade show-prompt oc1 --project jungle
cascade logs oc1 --project jungle --kind mandate
cascade run-agent oc1 --project jungle
cascade run-agent oc1 --project jungle --print-prompt
cascade preflight oc1 --project jungle
cascade mark oc1 --project jungle --state running
```

Recommended smoke test sequence:

```bash
make build
make test
```

## Running tests

```bash
make build   # build image once
make test    # run all tests (quiet)
```

Other test targets:

```bash
make test-verbose  # full -v output
make test-fast     # stop at first failure (-x)
make shell         # open a bash shell inside the container
```

Tests cover:

- Config/model profile parsing and helper resolution
- Cost estimation (token counting, USD arithmetic, format)
- Context pack building, truncation, blocked-path safety
- All deterministic no-model commands (`note`, `status`, `mark`, `diff`, `logs`, `capabilities`, `context-pack`, `estimate-cost`, `gate-summary`, `budget-status`)
- Model-backed command boundaries: OpenCode missing, launch prompt standards, configured `create_worktree` usage, `prepare-model-call` profile/model wiring
- Preflight/gate: configured command, exit-code-based pass/fail, log persistence, gate-failure classification (formatting vs typing vs security vs migration vs policy)
- Retry/budget tracking: attempt increment, attempt count, escalation threshold
- Standards preservation: launch prompt rules, output discipline rules per task type

Tests do **not** require: OpenCode, OpenRouter, Anthropic, GitHub, or a real jungle checkout.
Integration smoke tests (manual): run `cascade doctor --project-file examples/jungle.yaml` with a configured jungle checkout.

## Expected folder layout

```text
parent/
  cascade/
  jungle/
  jungle-worktrees/
  jungle-secrets/
```

## Project config behavior

Cascade is intentionally generic. The target repo provides behavior through YAML, including worktree creation and preflight commands.

For this MVP, project paths are resolved relative to the current working directory where you run `cascade`, not relative to the YAML file location. That keeps sibling paths like `../jungle` working when you run commands from the `cascade` repo root.

## OpenCode behavior

`cascade run-agent` launches OpenCode interactively in the target worktree.

This MVP does not try to pipe the generated launch prompt into OpenCode automatically. Instead, Cascade writes the prompt to the run directory and tells you where to paste or load it from.

## State layout

Cascade writes local state under `state/` in the current working directory:

```text
state/
  <project>/
    agents/
      <agent>.json
    runs/
      <agent>/
        mandate.md
        launch_prompt.md
        questions.md
        decisions.md
        running_summary.md
        transcript.md
        opencode_session_id.txt
        continue_prompt.md
        preflight.log
```

The state files do not store secrets.

## Commands

### Deterministic commands

- `doctor`
- `claim`
- `status`
- `show-prompt`
- `mark`
- `note`
- `context`
- `diff`
- `logs`
- `preflight`
- `capabilities`

### Model-backed commands

- `run-agent`
- `chat`
- `ask`
- `summarize`
- `continue`

### Planned model-backed commands

- `plan`
- `implement`
- `diagnose`
- `fix`
- `review`

### `claim`

Claims an issue for an agent, creates the worktree via the configured command, saves mandate and prompt files, and writes agent state.

## Cost control architecture

Cascade saves model spend by keeping most commands deterministic and routing model-backed work through cheap, bounded calls.

### Principles

- **Deterministic gates are the source of truth.** Model output never proves a gate passed. Only configured command exit codes do.
- **Context packs are bounded.** Each task type has a `max_input_tokens` budget. Sections are dropped in priority order before sending to any model.
- **Model profiles are explicit.** Each profile has a cost per million tokens so you can estimate spend before invoking a model.
- **Cost is estimated before calling.** `prepare-model-call` writes a prompt and metadata without any model call so you can sanity-check the cost first.
- **Cheap profiles route first.** Task types map to the cheapest qualified profile via `use_for` in YAML.
- **Retries are capped.** `retry_policy` in config limits attempts per task and controls when to escalate to a stronger model.
- **No full transcript by default.** Context packs include only a configurable tail of the transcript and a diff stat unless `include_full_diff` or `include_full_transcript` is set.
- **Simple pre-commit failures need no model.** `gate-summary` classifies failures as formatting, linting, migration, security, etc. and tells you whether a model call is likely needed.

### Recommended profile routing

| Task | Profile | Rationale |
|------|---------|-----------|
| plan, clarify, summarize | `cheap_planner` | Low cost, fast; planning does not need heavy reasoning |
| simple implementation, simple fix | `cheap_coder` | Low cost for well-scoped edits |
| complex implementation | `executor` | Higher accuracy for substantial changes |
| diagnosis, debug, review | `debugger` / `reviewer` | Reasoning-heavy; need stronger model |

### Deterministic cost-control commands

```bash
# Build a bounded context pack for the 'implement' task (no model call)
cascade context-pack oc1 --project jungle --task implement

# Estimate model cost before calling
cascade estimate-cost oc1 --project jungle --task implement --profile executor --expected-output-tokens 30000

# Prepare a prompt and cost metadata file for the 'diagnose' task
cascade prepare-model-call oc1 --project jungle --task diagnose --profile debugger

# Classify the latest gate failure without calling a model
cascade gate-summary oc1 --project jungle

# Show attempt counts, token estimates, gate state, and cost estimates
cascade budget-status oc1 --project jungle
```

All five commands work without OpenCode or model access. They read and write files under `state/<project>/runs/<agent>/`.

### Context pack state layout

```text
state/
  <project>/
    runs/
      <agent>/
        context_plan.md        # bounded context pack for 'plan' task
        context_plan.json      # metadata: tokens, budget, truncated flag
        context_implement.md
        context_implement.json
        plan_prompt.md         # full prompt ready to paste into OpenCode
        plan_model_call.json   # cost estimate and model id
        ...
```

### Configuring profiles and budgets in YAML

```yaml
models:
  default: openrouter/z-ai/glm-4.7-flash
  profiles:
    cheap_planner:
      provider: openrouter
      model: z-ai/glm-4.7-flash
      description: Cheap planner for low-stakes tasks
      input_cost_per_million: 0.06
      output_cost_per_million: 0.40
      use_for: [plan, clarify, summarize]
    executor:
      provider: openrouter
      model: z-ai/glm-4.7
      description: Reliable executor for implementation tasks
      input_cost_per_million: 0.38
      output_cost_per_million: 1.74
      use_for: [implement, implement_complex]

context_budgets:
  plan:
    max_input_tokens: 50000
    include_full_diff: false
    include_diff_stat: true
    include_logs_tail_lines: 50
    include_instruction_files: true
    include_full_transcript: false

retry_policy:
  cheap_coder_max_attempts: 2
  executor_max_attempts: 2
  debugger_max_attempts: 1
  same_gate_failure_escalation_after: 2
```

See `examples/jungle.yaml` for a complete working example.

### `run-agent`

Loads saved state and launches `opencode . --model <model>` in the assigned worktree with the selected prompt loaded automatically.

Defaults to `launch_prompt.md`, but you can override prompt selection with:

- `--task <task>` to use `<task>_prompt.md`
- `--prompt-file <path>` to use a specific file
- `--no-prompt` to launch OpenCode without injecting a prompt
- `--non-interactive` to run the prompt through `opencode run`

Use `--print-prompt` to print the prompt before launching. For host clipboard fallback, you can always run:

```bash
cascade show-prompt oc1 --project jungle | pbcopy
```

### `chat`

Starts an interactive OpenCode session for an agent and optional mode:

- `--mode plan` maps to OpenCode `--agent plan`
- `--mode build` maps to OpenCode `--agent build`

### `ask`

Sends a follow-up question through `opencode run` using capsule context, appends the exchange to `transcript.md`, and falls back safely if `--continue` is unsupported.

### `clarify`

Records user clarifications in `decisions.md` with timestamps, then asks OpenCode to update the plan and blocking questions.

### `summarize`

Requests a bounded summary using mandate, git status, recent transcript, and decisions, then writes `running_summary.md`.

### `continue`

Builds `continue_prompt.md` from the capsule and launches OpenCode with that continuation prompt loaded automatically. Use `--print-prompt` to print it before launching.

### `status`

Shows all claimed agents for a project in a Rich table.

### `show-prompt`

Prints the generated launch prompt for easy copy/paste. You can also pass `--task <task>` or `--prompt-file <path>`.

### `mark`

Updates the saved agent lifecycle state.

Allowed states:

- `claimed`
- `running`
- `blocked`
- `implementation_done`
- `preflight_running`
- `preflight_failed`
- `preflight_passed`
- `closeout_ready`
- `closed`

### `doctor`

Checks Python, required CLIs, GitHub auth, and the configured project paths without modifying files.

When Cascade is running in Docker, `doctor` also checks that host-Docker access is available by verifying:

- the Docker CLI exists inside the Cascade container
- `/var/run/docker.sock` is mounted and readable
- `docker info` can reach the host Docker daemon

### `preflight`

Runs the configured preflight command in the assigned worktree, saves output to `preflight.log`, and updates the saved agent state.

For Docker-backed target repos, preflight runs through the host Docker daemon mounted into the Cascade container. This keeps commands like `make mandate-preflight` and `docker compose` working from `/workspace/<repo>` and `/workspace/<repo>-worktrees/...`.

### `logs`

Prints `preflight`, `prompt`, or `mandate` content from the saved run directory.

### `capabilities`

Prints command category and capability metadata, including whether a command is deterministic, model-backed, or planned.

## Standards preservation

Cascade is intended to maintain or exceed the workflow standards already defined by target repo instruction files.

For the `jungle` example, Cascade uses configured Make and linked-worktree flows instead of bypassing with ad-hoc shortcuts. It does not weaken gates, does not auto-commit, does not treat model responses as validation, and keeps configured preflight command exit codes plus saved logs as the source of truth.

## Conversational workflow

1. Claim mandate:

```bash
cascade claim --project-file examples/jungle.yaml --issue 45 --agent oc1 --model openrouter/z-ai/glm-4.7-flash
```

2. Start the agent session:

```bash
cascade run-agent oc1 --project jungle
```

3. Start plan chat:

```bash
cascade chat oc1 --project jungle --mode plan
```

4. Add clarification:

```bash
cascade clarify oc1 --project jungle --message "Use Django Constance for this flag."
```

5. Ask status:

```bash
cascade ask oc1 --project jungle "What are the remaining blockers?"
```

6. Summarize:

```bash
cascade summarize oc1 --project jungle
```

7. Continue later:

```bash
cascade continue oc1 --project jungle
```

Clipboard fallback when you want to paste manually:

```bash
cascade show-prompt oc1 --project jungle | pbcopy
```

## Deterministic-first workflow

No-model setup:

```bash
cascade doctor --project-file examples/jungle.yaml
cascade claim --project-file examples/jungle.yaml --issue 45 --agent oc1 --model openrouter/z-ai/glm-4.7-flash
cascade note oc1 --project jungle --message "Use existing notification preferences if possible."
cascade context oc1 --project jungle --print
cascade diff oc1 --project jungle
cascade status --project jungle
```

Model-backed step only when ready:

```bash
cascade run-agent oc1 --project jungle
```

Then no-model validation:

```bash
cascade preflight oc1 --project jungle
cascade logs oc1 --project jungle --kind preflight
```

## Troubleshooting Docker-backed target repos

If OpenCode inside the Cascade container cannot run target repo tests or preflight commands such as `make mandate-preflight`, `docker compose`, or other Docker-backed Make targets, verify host-daemon access first.

Expected setup:

- the Cascade image includes the Docker CLI and Compose plugin
- `docker-compose.yml` mounts `/var/run/docker.sock:/var/run/docker.sock`
- Docker commands inside Cascade use the host Docker daemon
- Cascade does not run privileged Docker-in-Docker and does not start a nested daemon

Useful checks:

```bash
cascade doctor --project-file examples/jungle.yaml
docker compose run --rm cascade docker info
docker compose run --rm cascade docker compose version
```

If those fail:

- rebuild the image so the Docker CLI and Compose plugin are present: `make rebuild`
- confirm the host Docker daemon is running
- confirm `docker-compose.yml` still mounts `/var/run/docker.sock:/var/run/docker.sock`
- rerun `cascade doctor` and make sure `docker CLI`, `docker socket`, and `docker info` are all `ok`

Once those checks pass, commands launched from paths such as `/workspace/jungle-worktrees/<agent-slug>` can call the host daemon through the target repo's normal `make` and `docker compose` workflows.

Future model-backed diagnosis:

```bash
cascade diagnose oc1 --project jungle
```

## Limitations

- GitHub issue access uses the local `gh` CLI only.
- Worktree path resolution is convention-based for now: `<worktree_root>/<agent>-<slug>`.
- Worktree detection now checks a small set of safe candidate paths, but it is still heuristic.
- Closeout or done commands are still not automated.
- OpenCode session IDs are stored as placeholders for now; automatic session capture is not implemented in this pass.