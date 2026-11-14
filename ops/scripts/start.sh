#!/usr/bin/env bash
# Start MiroFish (Flask backend, optionally the Vite frontend) bound to 127.0.0.1 only.
# Usage: ops/scripts/start.sh [--no-frontend] [--netaudit FILE]
#   --netaudit FILE   log every Python-level connect/DNS lookup of the backend and its
#                     simulation subprocesses to FILE (JSON lines; used by netcheck.sh)
# When LLM_BASE_URL points at 127.0.0.1:$LLM_METER_PORT, the metering proxy (llm_meter.py) is
# started in front of Ollama; it logs per-call metadata (no content) to ops/logs/llm_meter.jsonl.
# PIDs go to ops/state/{backend,frontend,meter}.pid, console output to ops/logs/. Stop with stop.sh.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=_common.sh
source "$OPS_DIR/scripts/_common.sh"

WITH_FRONTEND=1
NETAUDIT_LOG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-frontend) WITH_FRONTEND=0 ;;
        --netaudit) NETAUDIT_LOG="${2:?--netaudit needs a file path}"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

umask 077
UPSTREAM="$PROJECT_ROOT/mirofish"
ENV_FILE="$UPSTREAM/.env"
PYTHON="$UPSTREAM/backend/.venv/bin/python"
VITE="$UPSTREAM/frontend/node_modules/.bin/vite"
STATE="$OPS_DIR/state"
LOGS="$OPS_DIR/logs"
FRONTEND_PORT=3000
STARTED=0

fail() {
    echo "FAIL: $*" >&2
    [[ "$STARTED" -eq 1 ]] && "$OPS_DIR/scripts/stop.sh" >/dev/null 2>&1
    exit 1
}
env_get() { python3 "$OPS_DIR/scripts/envfile.py" get "$ENV_FILE" "$1" 2>/dev/null || true; }
is_loopback() { [[ "$1" == "127.0.0.1" || "$1" == "localhost" || "$1" == "::1" ]]; }
url_host() { python3 -c 'import sys, urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).hostname or "")' "$1"; }
url_port() { python3 -c 'import sys, urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).port or "")' "$1"; }
alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }
listeners() { lsof -nP -a -p "$1" -iTCP -sTCP:LISTEN -Fn 2>/dev/null | sed -n 's/^n//p' | sort -u; }
port_in_use() { lsof -nP -t -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

command -v lsof >/dev/null 2>&1 || fail "lsof is required to verify the listening addresses"
[[ -x "$PYTHON" && -f "$ENV_FILE" ]] || fail "run ops/scripts/bootstrap.sh first"
for pidfile in "$STATE/backend.pid" "$STATE/frontend.pid" "$STATE/meter.pid"; do
    alive "$pidfile" && fail "already running ($pidfile); run ops/scripts/stop.sh first"
done

# --- Configuration guards. The backend loads .env with override=True, so .env is authoritative. ---
FLASK_HOST="$(env_get FLASK_HOST)"
FLASK_PORT="$(env_get FLASK_PORT)"; FLASK_PORT="${FLASK_PORT:-5001}"
is_loopback "$FLASK_HOST" || fail "FLASK_HOST in $ENV_FILE must be 127.0.0.1 (got '${FLASK_HOST}')"
[[ "$(env_get FLASK_DEBUG | tr '[:upper:]' '[:lower:]')" != "true" ]] || fail "FLASK_DEBUG must be False"
llm_url="$(env_get LLM_BASE_URL)"
[[ -n "$llm_url" ]] || fail "LLM_BASE_URL is empty in $ENV_FILE (upstream would fall back to api.openai.com)"
METER_PORT="${LLM_METER_PORT:-11435}"  # from model.conf; read before the environment scrub below
for key in LLM_BASE_URL LLM_BOOST_BASE_URL LOCAL_GRAPH_EMBED_URL; do
    value="$(env_get "$key")"
    [[ -z "$value" ]] || is_loopback "$(url_host "$value")" || fail "$key in $ENV_FILE is not a loopback URL"
done
# Any other memory backend is upstream's Zep Cloud, which would upload the seed (ops/ZEP_DECISION.md).
[[ "$(env_get MIROFISH_MEMORY_BACKEND)" == "local" ]] \
    || fail "MIROFISH_MEMORY_BACKEND in $ENV_FILE must be 'local'; anything else sends the seed to Zep Cloud"
forbidden="$(python3 - "$ENV_FILE" "$OPS_DIR/scripts" <<'EOF'
import re, sys
sys.path.insert(0, sys.argv[2])
from envfile import parse, read_lines
from pathlib import Path
tracing = re.compile(r"^(LANGCHAIN_|LANGSMITH_|AGENTOPS_|LANGFUSE_|HELICONE_|WANDB_|OTEL_EXPORTER_)")
values = parse(read_lines(Path(sys.argv[1])))
print(" ".join(k for k, v in values.items()
               if tracing.match(k) and v.strip().lower() not in {"", "false", "0", "no", "off"}))
EOF
)"
[[ -z "$forbidden" ]] || fail "tracing/telemetry keys enabled in $ENV_FILE: $forbidden"
if [[ "$(env_get HF_HUB_OFFLINE)" != "1" ]]; then
    echo "WARN: HF_HUB_OFFLINE is not 1; run ops/scripts/cache_assets.sh or the Twitter simulation will download from huggingface.co" >&2
fi
sync_reason="$(cloud_sync_reason)"
[[ -z "$sync_reason" ]] || echo "WARN: project folder looks cloud-synced: $sync_reason." \
    "Seeds, runs and mappings would leave this machine; move the project or exclude it from sync." >&2

# --- Scrub the process environment: only .env and this script decide what the backend sees. ---
while IFS= read -r name; do
    unset "$name"
done < <(compgen -e | grep -E '^(LANGCHAIN_|LANGSMITH_|AGENTOPS_|LANGFUSE_|HELICONE_|WANDB_|OTEL_|LLM_|OPENAI_|ZEP_|LOCAL_GRAPH_|MIROFISH_|FLASK_|HF_|TRANSFORMERS_|TIKTOKEN_|HTTPS?_PROXY$|ALL_PROXY$|https?_proxy$|all_proxy$)' || true)
export NO_PROXY="127.0.0.1,localhost,::1" no_proxy="127.0.0.1,localhost,::1"
export HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 ANONYMIZED_TELEMETRY=False
export PYTHONPATH="$OPS_DIR/runtime"
if [[ -n "$NETAUDIT_LOG" ]]; then
    # Absolute: the backend and each simulation run in different working directories.
    NETAUDIT_LOG="$(cd "$(dirname "$NETAUDIT_LOG")" && pwd)/$(basename "$NETAUDIT_LOG")"
    : >"$NETAUDIT_LOG"
    chmod 600 "$NETAUDIT_LOG"
    export MIROFISH_NETAUDIT_LOG="$NETAUDIT_LOG"
fi

make_private_dir "$STATE"
make_private_dir "$LOGS"
make_private_dir "$UPSTREAM/backend/uploads"
make_private_dir "$UPSTREAM/backend/logs"

WITH_METER=0
if [[ "$(url_port "$llm_url")" == "$METER_PORT" ]]; then
    WITH_METER=1
    port_in_use "$METER_PORT" && fail "port $METER_PORT (LLM meter) is already in use"
else
    echo "WARN: LLM_BASE_URL does not use the meter port $METER_PORT; LLM calls are not measured" \
         "and run_batch.py will refuse to run (set LLM_BASE_URL=http://127.0.0.1:$METER_PORT/v1)" >&2
fi
port_in_use "$FLASK_PORT" && fail "port $FLASK_PORT is already in use"
[[ "$WITH_FRONTEND" -eq 0 ]] || ! port_in_use "$FRONTEND_PORT" || fail "port $FRONTEND_PORT is already in use"

"$OPS_DIR/scripts/ollama_serve.sh"
ollama_pid="$(lsof -nP -t -iTCP:"${OLLAMA_HOST##*:}" -sTCP:LISTEN 2>/dev/null | head -1)"
[[ -n "$ollama_pid" ]] || fail "nothing is listening on $OLLAMA_HOST"
bad="$(listeners "$ollama_pid" | grep -vE '^(127\.0\.0\.1|\[::1\]):[0-9]+$' || true)"
[[ -z "$bad" ]] || fail "Ollama listens on a non-loopback address: $bad"
# Inspect the environment of whichever Ollama is listening (ps eww shows it for our own processes).
if ps eww -p "$ollama_pid" 2>/dev/null | tr ' ' '\n' | grep -qx 'OLLAMA_NO_CLOUD=1'; then
    echo "$ollama_pid" >"$STATE/ollama.pid"
else
    echo "WARN: the Ollama on $OLLAMA_HOST (pid $ollama_pid) was not started with OLLAMA_NO_CLOUD=1;" \
         "stop it and re-run so ops/scripts/ollama_serve.sh starts it with cloud features off." >&2
fi

# Job control gives each background job its own process group, so stop.sh can signal the group.
set -m

wait_http() {
    local url="$1" pidfile="$2" name="$3"
    for _ in $(seq 1 60); do
        curl --noproxy '*' -fsS --max-time 2 -o /dev/null "$url" 2>/dev/null && return 0
        alive "$pidfile" || fail "$name exited during startup; see $LOGS/$name.log"
        sleep 1
    done
    fail "$name did not answer at $url within 60s; see $LOGS/$name.log"
}

# Fail (and stop everything) unless the process listens, and only on loopback.
check_listeners() {
    local pid="$1" name="$2" found bad
    found="$(listeners "$pid")"
    [[ -n "$found" ]] || fail "$name (pid $pid) has no listening socket; another process may own the port"
    bad="$(echo "$found" | grep -vE '^(127\.0\.0\.1|\[::1\]):[0-9]+$' || true)"
    [[ -z "$bad" ]] || fail "$name listens on a non-loopback address: $bad"
    echo "OK: $name listens only on: $(echo "$found" | tr '\n' ' ')"
}

STARTED=1
if [[ "$WITH_METER" -eq 1 ]]; then
    python3 "$OPS_DIR/scripts/llm_meter.py" --listen "127.0.0.1:$METER_PORT" --upstream "$OLLAMA_HOST" \
        --log "$LOGS/llm_meter.jsonl" </dev/null >>"$LOGS/llm_meter.log" 2>&1 &
    echo $! >"$STATE/meter.pid"
    wait_http "http://127.0.0.1:$METER_PORT/meter/health" "$STATE/meter.pid" llm_meter
    check_listeners "$(cat "$STATE/meter.pid")" llm_meter
fi
(cd "$UPSTREAM/backend" && exec "$PYTHON" run.py) </dev/null >>"$LOGS/backend.log" 2>&1 &
echo $! >"$STATE/backend.pid"
wait_http "http://127.0.0.1:$FLASK_PORT/health" "$STATE/backend.pid" backend
check_listeners "$(cat "$STATE/backend.pid")" backend
echo "Backend:  http://127.0.0.1:$FLASK_PORT (pid $(cat "$STATE/backend.pid"))"

if [[ "$WITH_FRONTEND" -eq 1 ]]; then
    [[ -x "$VITE" ]] || fail "frontend dependencies missing; run ops/scripts/bootstrap.sh"
    (cd "$UPSTREAM/frontend" && \
        VITE_API_BASE_URL="http://127.0.0.1:$FLASK_PORT" BROWSER=none \
        exec "$VITE" --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort) </dev/null >>"$LOGS/frontend.log" 2>&1 &
    echo $! >"$STATE/frontend.pid"
    wait_http "http://127.0.0.1:$FRONTEND_PORT/" "$STATE/frontend.pid" frontend
    check_listeners "$(cat "$STATE/frontend.pid")" frontend
    echo "Frontend: http://127.0.0.1:$FRONTEND_PORT (pid $(cat "$STATE/frontend.pid"))"
fi
