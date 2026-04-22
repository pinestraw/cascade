#!/usr/bin/env bash
set -euo pipefail

CASCADE_REPO="__CASCADE_REPO__"

if [[ ! -d "$CASCADE_REPO" ]]; then
  echo "Cascade repo not found: $CASCADE_REPO" >&2
  exit 1
fi

cd "$CASCADE_REPO"
exec docker compose run --rm cascade cascade "$@"
