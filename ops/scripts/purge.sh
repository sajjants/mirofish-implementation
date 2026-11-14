#!/usr/bin/env bash
# Securely delete a run's outputs, graph data and evidence. See purge.py for what is removed.
# Usage: ops/scripts/purge.sh RUN_ID [--logs] [--yes]
#   --logs  also shred every backend and ops log file (requires MiroFish stopped)
#   --yes   skip the confirmation prompt
# Works with MiroFish running (graph deleted through the API) or stopped (deleted directly).
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$OPS_DIR/scripts/purge.py" "$@"
