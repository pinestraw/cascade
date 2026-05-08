# Jungle Compliance Contract For Cascade

This file defines mandatory invariants Cascade must uphold when orchestrating Jungle mandates.

## Branch And Worktree Invariants

- Agent branch must be exact: `agent/{agent}/{slug}`.
- Worktree must exist and be inside configured `paths.worktree_root`.
- Cascade must reject branch-prefix fallbacks (for example any generic `agent/*`).

## Mandate Metadata Invariants

- Metadata file: `.github/mandates/{slug}.json` must exist before preflight.
- Required correctness:
  - `agent_branch` matches exact branch template.
  - `active_branch` matches configured active branch.
  - `mandate_id` exists and is non-empty.
  - `canonical_mandate` exists and is non-empty.
  - `worktree_path` matches the assigned worktree.
  - `repo` matches project repo name.

## Preflight Invariants

- Preflight command must come from config (`commands.preflight`), not hardcoded.
- Pass/fail is determined by process exit code.
- Changed-line policy and coverage gates remain strict.
- Scope behavior in Jungle scripts must remain:
  - backend tests run only when backend files are in file_scope.
  - frontend phases run only when `web/*` is in scope.
  - mutation gate runs only for `jungle/utils/*.py` scope.
  - empty scope remains conservative.

## Loop Invariants

- Loop is controller of record; model narration never counts as success.
- Loop reruns preflight after each fix attempt.
- Before and after model fix attempt, loop must validate branch identity.
- Branch switch/rename during model run is an immediate workflow violation stop.
- Deterministic workflow/environment failures do not spend model budget by default.

## Closeout Invariants

- `finish` only verifies readiness and marks `closeout_ready`.
- `closeout` executes configured closeout command (`commands.done`, Jungle: `make mandate-done ...`).
- `closeout` requires passing fresh preflight.
- `closeout` records closeout metadata, transitions to `closed` on success.
- `commands.propagate` is best-effort post-closeout and must not hide failures.

## GitHub Project Sync Invariants

- On claim/start: set item status to in_progress and write Mandate ID field when project config exists.
- On closeout: set item status to done.
- Missing token/scope must degrade with explicit warning, never silent success.

## Host Native And Docker Portability Invariants

- Cascade must detect and migrate Docker-era state paths (`/workspace/...`) for host-native use.
- Doctor must report stale Docker-era persisted state and broken workspace links.
- Path handling must fail clearly on unresolved host/docker mismatch.

## Stop Conditions

- `precommit_failures >= 3` requires human intervention.
- Security/policy/migration approval categories stop automatically.
- Repeated identical failure after deterministic repair stops automatically.
