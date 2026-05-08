#!/usr/bin/env bash
set -euo pipefail

# Deterministic helper smoke for docker-runtime-network handling.
# This does not depend on flaky external networking.
# It seeds a synthetic preflight log and runs dry-run repair.

PROJECT="${PROJECT:-jungle}"
AGENT="${AGENT:-smoke-agent}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-jungle-simulated}"

STATE_FILE="state/${PROJECT}/agents/${AGENT}.json"
RUN_DIR="state/${PROJECT}/runs/${AGENT}"
PREFLIGHT_LOG="${RUN_DIR}/preflight.log"

if [[ ! -f "$STATE_FILE" ]]; then
  echo "SKIP: missing agent state at $STATE_FILE"
  echo "Run a claim/start first, then re-run this helper."
  exit 0
fi

mkdir -p "$RUN_DIR"
cat > "$PREFLIGHT_LOG" <<EOF
Error response from daemon: error while removing network: network ${COMPOSE_PROJECT}_default has active endpoints
EOF

cascade repair "$AGENT" --project "$PROJECT" --kind docker-runtime-network --dry-run

echo "OK: simulated docker-runtime-network dry-run repair completed"
