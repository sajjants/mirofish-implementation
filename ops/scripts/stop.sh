#!/usr/bin/env bash
# Stop the MiroFish frontend, backend and LLM meter started by start.sh, including simulation
# subprocesses (which run in their own sessions). Ollama is left running unless --ollama.
# Usage: ops/scripts/stop.sh [--ollama]
set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$OPS_DIR/state"
names=(frontend backend meter)
[[ "${1:-}" == "--ollama" ]] && names+=(ollama)

descendants() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do
        echo "$child"
        descendants "$child"
    done
}

# A PID file can outlive its process and the PID be reused, so only signal a process whose
# command line still matches what start.sh / ollama_serve.sh launched.
expected_command() {
    case "$1" in
        backend) echo "run.py" ;;
        frontend) echo "vite" ;;
        meter) echo "llm_meter.py" ;;
        ollama) echo "ollama serve" ;;
    esac
}

stop_one() {
    local name="$1" pidfile="$STATE/$1.pid" pid tree command
    [[ -f "$pidfile" ]] || return 0
    pid="$(cat "$pidfile")"
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ -z "$command" ]]; then
        if kill -0 "$pid" 2>/dev/null; then
            # Alive but not inspectable (e.g. ps restricted by a sandbox): keep the PID file.
            echo "$name: pid $pid is running but its command line cannot be read; not stopped" >&2
            failed=1
            return 0
        fi
        rm -f "$pidfile"
        return 0
    fi
    if [[ "$command" != *"$(expected_command "$name")"* ]]; then
        echo "$name: pid $pid is now '$command', not ours; removing stale $pidfile without signalling" >&2
        rm -f "$pidfile"
        return 0
    fi
    tree="$(descendants "$pid")"
    # SIGTERM lets the backend run its own cleanup (it terminates simulation process groups).
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    local leftover=""
    for p in $pid $tree; do
        kill -0 "$p" 2>/dev/null && leftover+="$p "
    done
    if [[ -n "$leftover" ]]; then
        # shellcheck disable=SC2086
        kill -KILL $leftover 2>/dev/null
        echo "$name: force-killed $leftover"
    fi
    rm -f "$pidfile"
    echo "$name stopped (pid $pid)"
}

failed=0
for name in "${names[@]}"; do
    stop_one "$name"
done
exit "$failed"
