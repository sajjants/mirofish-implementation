#!/usr/bin/env bash
# Start the Ollama server on loopback only, with cloud features disabled.
# Usage: ops/scripts/ollama_serve.sh [--foreground]
# Background mode writes the PID to ops/state/ollama.pid and logs to ops/logs/ollama.log.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=_common.sh
source "$OPS_DIR/scripts/_common.sh"

bin="$(resolve_ollama_bin)"
if [[ -z "$bin" ]]; then
    echo "ERROR: no Ollama binary found (see ops/README.md)." >&2
    exit 1
fi

case "${OLLAMA_HOST%:*}" in
    127.0.0.1|localhost) ;;
    *) echo "ERROR: OLLAMA_HOST must be loopback (got $OLLAMA_HOST)." >&2; exit 1 ;;
esac

if curl --noproxy '*' -fsS --max-time 2 "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
    echo "Ollama already running at http://$OLLAMA_HOST"
    exit 0
fi

export OLLAMA_NO_CLOUD=1
export OLLAMA_NUM_PARALLEL OLLAMA_KEEP_ALIVE
export OLLAMA_FLASH_ATTENTION=1
# Never route Ollama's own traffic (model pulls) through an inherited proxy setting silently.
unset ALL_PROXY all_proxy

if [[ "${1:-}" == "--foreground" ]]; then
    exec "$bin" serve
fi

make_private_dir "$OPS_DIR/logs"
make_private_dir "$OPS_DIR/state"
nohup "$bin" serve >>"$OPS_DIR/logs/ollama.log" 2>&1 &
echo $! > "$OPS_DIR/state/ollama.pid"

for _ in $(seq 1 30); do
    if curl --noproxy '*' -fsS --max-time 2 "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
        echo "Ollama started (pid $(cat "$OPS_DIR/state/ollama.pid")) at http://$OLLAMA_HOST"
        exit 0
    fi
    sleep 1
done
echo "ERROR: Ollama did not become reachable; see ops/logs/ollama.log" >&2
exit 1
