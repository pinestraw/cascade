#!/usr/bin/env bash
set -euo pipefail

# GitHub Project sync smoke flow (requires GH token with project scope).

PROJECT_FILE="${PROJECT_FILE:-examples/jungle.yaml}"
AGENT="${AGENT:-smoke-gh}"

if [[ -z "${ISSUE:-}" ]]; then
	echo "SKIP: set ISSUE=<real-jungle-issue-number> to run GitHub project sync smoke."
	exit 0
fi

cascade claim --project-file "$PROJECT_FILE" --issue "$ISSUE" --agent "$AGENT"
