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
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

If `python3` points to an older interpreter on your machine, use an explicit 3.11+ binary such as `python3.11` instead.

For local development and tests:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

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
cascade --help
cascade doctor --project-file examples/jungle.yaml
pytest
```

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