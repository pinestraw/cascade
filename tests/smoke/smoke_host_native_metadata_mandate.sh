#!/usr/bin/env bash
set -euo pipefail

# Host-native metadata-only smoke flow.
# Requires existing local setup and valid project file.
# Idempotency: this script removes prior smoke-agent worktree/state so it can
# be re-run back-to-back without manual cleanup.

PROJECT_FILE="${PROJECT_FILE:-examples/jungle.yaml}"
AGENT="${AGENT:-smoke-agent}"
FAST_PREFLIGHT="${FAST_PREFLIGHT:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CASCADE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATE_ROOT="$CASCADE_ROOT/state/jungle"
AGENT_STATE_FILE="$STATE_ROOT/agents/$AGENT.json"
AGENT_RUN_DIR="$STATE_ROOT/runs/$AGENT"
JUNGLE_REPO_ROOT="$(cd "$CASCADE_ROOT/../jungle" && pwd)"
JUNGLE_WORKTREE_ROOT="$(cd "$CASCADE_ROOT/../jungle-worktrees" && pwd)"

cleanup_previous_smoke_state() {
	local prior_worktree=""
	local line=""
	local registered_worktree=""
	if [[ -f "$AGENT_STATE_FILE" ]]; then
		prior_worktree="$(grep -E '"worktree"[[:space:]]*:' "$AGENT_STATE_FILE" | head -1 | sed -E 's/.*"worktree"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
	fi

	# Remove the exact prior smoke worktree if recorded and still present.
	if [[ -n "$prior_worktree" ]]; then
		if [[ "$prior_worktree" == "$JUNGLE_WORKTREE_ROOT"/* ]]; then
			git -C "$JUNGLE_REPO_ROOT" worktree remove --force "$prior_worktree" >/dev/null 2>&1 || true
		fi
		if [[ "$prior_worktree" == "$JUNGLE_WORKTREE_ROOT"/* ]] && [[ -e "$prior_worktree" ]]; then
			echo "Removing prior smoke worktree: $prior_worktree"
			rm -rf "$prior_worktree"
		fi
	fi

	# Remove any git-registered worktree matching this smoke agent prefix.
	while IFS= read -r line; do
		if [[ "$line" == worktree\ * ]]; then
			registered_worktree="${line#worktree }"
			if [[ "$registered_worktree" == "$JUNGLE_WORKTREE_ROOT"/"$AGENT"-* ]]; then
				echo "Removing registered smoke worktree: $registered_worktree"
				git -C "$JUNGLE_REPO_ROOT" worktree remove --force "$registered_worktree" >/dev/null 2>&1 || true
				rm -rf "$registered_worktree"
			fi
		fi
	done < <(git -C "$JUNGLE_REPO_ROOT" worktree list --porcelain)

	# Remove any remaining smoke worktrees for this agent under jungle-worktrees.
	for candidate in "$JUNGLE_WORKTREE_ROOT"/"$AGENT"-*; do
		if [[ -d "$candidate" ]]; then
			echo "Removing stale smoke worktree: $candidate"
			rm -rf "$candidate"
		fi
	done

	# Prune stale worktree metadata from the jungle repo.
	git -C "$JUNGLE_REPO_ROOT" worktree prune

	# Remove matching Cascade state for this smoke agent.
	rm -f "$AGENT_STATE_FILE"
	rm -rf "$AGENT_RUN_DIR"
}

cd "$CASCADE_ROOT"

if [[ "${RUN_HOST_NATIVE_SMOKE:-0}" != "1" ]]; then
	echo "SKIP: set RUN_HOST_NATIVE_SMOKE=1 to run host-native mandate lifecycle smoke."
	exit 0
fi

ISSUE="${ISSUE:?Set ISSUE to a real Jungle issue number}"

cleanup_previous_smoke_state

cascade start "$ISSUE" --agent "$AGENT" --project-file "$PROJECT_FILE" --no-launch
if [[ "$FAST_PREFLIGHT" == "1" ]]; then
	echo "FAST_PREFLIGHT=1: using smoke-only fast preflight overrides"
	MANDATE_PREFLIGHT_HEAVY_LOCK_TIMEOUT=45 \
	MANDATE_PREFLIGHT_BACKEND_TEST_CMD=true \
	MANDATE_PREFLIGHT_FRONTEND_TEST_CMD=true \
	MANDATE_PREFLIGHT_MUTATION_CMD=true \
		cascade check "$AGENT" --project jungle
else
	cascade check "$AGENT" --project jungle
fi
cascade finish "$AGENT" --project jungle --no-dry-run --yes
cascade closeout "$AGENT" --project jungle --yes
