#!/usr/bin/env bash
set -euo pipefail

# Docker-backed smoke flow for cascade container wiring.

make rebuild
make test-fast
