#!/usr/bin/env bash
set -euo pipefail

# Docker-era -> host-native migration smoke flow.

PROJECT="${PROJECT:-jungle}"
AGENT="${AGENT:-smoke-migrate}"
STATE_FILE="state/${PROJECT}/agents/${AGENT}.json"

mkdir -p "$(dirname "$STATE_FILE")"
cat > "$STATE_FILE" <<'JSON'
{
  "project": "jungle",
  "agent": "smoke-migrate",
  "project_file": "/workspace/cascade/examples/jungle.yaml",
  "worktree": "/workspace/jungle-worktrees/smoke-migrate-test"
}
JSON

cascade doctor --project-file examples/jungle.yaml || true
cascade repair "$AGENT" --project "$PROJECT" --kind docker-era-state
