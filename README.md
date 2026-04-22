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
3. Review the generated launch prompt with `cascade show-prompt`.
4. Start the agent session with `cascade run-agent`.
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
make build             # build the Docker image once
```

For running Cascade locally (outside Docker), you still need a 3.11+ Python and can install with:

```bash
pip install -e .
```

## Running with Docker Compose

Docker is the preferred reproducible workflow.  The container includes Python,
Node.js, OpenCode (`opencode-ai`), the GitHub CLI (`gh`), and Cascade itself.

### Expected folder layout

```text
parent/
  cascade/           ← this repo
  jungle/
  jungle-worktrees/
  jungle-secrets/
```

The Compose volume mounts `..:/workspace` so all sibling repos are visible
inside the container at `/workspace/<repo-name>`.  Paths like `../jungle` in
`examples/jungle.yaml` resolve correctly inside the container.

### First-time setup

```bash
cp .env.example .env
# Add OPENROUTER_API_KEY (and GITHUB_TOKEN) to .env

make build
make shell        # opens bash inside the container
cascade --help
opencode --version
```

### API keys

**Option A** (recommended): set keys in `.env` before `make build`.

```ini
OPENROUTER_API_KEY=sk-or-...
GITHUB_TOKEN=ghp_...
```

**Option B**: run `make shell`, then inside the container run `opencode` and
use `/connect` to authenticate interactively.  Auth persists in the
`opencode-data` Docker volume.

**GitHub CLI**: either set `GITHUB_TOKEN` in `.env`, or run `gh auth login`
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
make check AGENT=oc1 PROJECT=jungle
make finish AGENT=oc1 PROJECT=jungle          # dry-run (safe preview)
make finish AGENT=oc1 PROJECT=jungle YES=1    # actually close out
```

### Other workflow targets

```bash
make status  PROJECT=jungle
make logs    AGENT=oc1 PROJECT=jungle KIND=preflight
make context AGENT=oc1 PROJECT=jungle TASK=implement
make estimate AGENT=oc1 PROJECT=jungle TASK=implement PROFILE=executor OUT=30000
make prepare  AGENT=oc1 PROJECT=jungle TASK=implement PROFILE=executor
```

### Run OpenCode interactively in a worktree

```bash
make opencode WORKTREE=../jungle-worktrees/oc1-daily-digest-email MODEL=openrouter/z-ai/glm-4.7
```

If `WORKTREE` is omitted a plain bash shell opens instead so you can run
`opencode <path>` manually.

### Limitations

- `make finish YES=1` passes `--yes` to `cascade finish`; verify your version
  of Cascade supports that flag before using it.
- `~/.ssh` and `~/.gitconfig` are mounted read-only; adjust the volume entries
  in `docker-compose.yml` if your paths differ.
- GitHub CLI interactive `gh auth login` works inside `make shell` but auth is
  not persisted across containers unless you add a dedicated Docker volume for
  `~/.config/gh`.

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

Loads saved state and launches `opencode . --model <model>` in the assigned worktree.

On macOS it also prints a `pbcopy` command for the generated prompt, and `--print-prompt` will print the prompt before launching OpenCode.

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

Builds `continue_prompt.md` from the capsule and launches OpenCode interactively. Use `--print-prompt` to print it before launching.

### `status`

Shows all claimed agents for a project in a Rich table.

### `show-prompt`

Prints the generated launch prompt for easy copy/paste.

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

### `preflight`

Runs the configured preflight command in the assigned worktree, saves output to `preflight.log`, and updates the saved agent state.

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

2. Show launch prompt:

```bash
cascade show-prompt oc1 --project jungle
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
cascade continue oc1 --project jungle --print-prompt
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

Future model-backed diagnosis:

```bash
cascade diagnose oc1 --project jungle
```

## Limitations

- GitHub issue access uses the local `gh` CLI only.
- Worktree path resolution is convention-based for now: `<worktree_root>/<agent>-<slug>`.
- Worktree detection now checks a small set of safe candidate paths, but it is still heuristic.
- OpenCode prompt injection is manual.
- Closeout or done commands are still not automated.
- OpenCode session IDs are stored as placeholders for now; automatic session capture is not implemented in this pass.