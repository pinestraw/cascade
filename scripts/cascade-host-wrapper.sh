#!/usr/bin/env bash
set -euo pipefail

CASCADE_REPO="__CASCADE_REPO__"
CASCADE_PYTHON="__CASCADE_PYTHON__"
CASCADE_WRAPPER_KIND="host"

if [[ ! -d "$CASCADE_REPO" ]]; then
  echo "Cascade repo not found: $CASCADE_REPO" >&2
  exit 1
fi

if ! command -v "$CASCADE_PYTHON" >/dev/null 2>&1; then
  echo "Configured Python is unavailable: $CASCADE_PYTHON" >&2
  echo "Re-run: make install-host PYTHON=<python3.12-or-venv-python>" >&2
  exit 1
fi

cd "$CASCADE_REPO"
exec "$CASCADE_PYTHON" -m cascade.cli "$@"
