#!/usr/bin/env bash
# Run a pilot with network auditing on and fail if any MiroFish process talks to a
# non-loopback host that is not listed in ops/config/allowed_hosts.txt, or if Ollama
# opens any non-loopback socket while the pilot runs.
#
# Usage: ops/scripts/netcheck.sh [--run-id ID] [--seed FILE] [--requirement-file FILE]
#                                [--rounds 10] [--platform parallel] [--purge]
# Defaults use the synthetic fixture seed in ops/tests/fixtures/ and a 10-round pilot on
# both platforms (Twitter exercises the Hugging Face recommender model, Reddit does not).
#
# Exit status: 0 PASS; 1 FAIL (a violation, or missing evidence that the audit ran);
#              2 INCONCLUSIVE (network clean, but the pilot did not complete).
# Evidence (all 0600, under ops/runs/<RUN_ID>/):
#   netaudit.jsonl  every Python-level connect/send/DNS lookup by the backend and simulations
#   lsof.jsonl      internet sockets of the backend, frontend, their descendants and Ollama, every second
#   netcheck.json   the evaluation
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=_common.sh
source "$OPS_DIR/scripts/_common.sh"

RUN_ID="netcheck-$(date +%Y%m%d-%H%M%S)"
SEED="$OPS_DIR/tests/fixtures/synthetic_seed.md"
REQUIREMENT="$OPS_DIR/tests/fixtures/synthetic_requirement.txt"
ROUNDS=10
PLATFORM=parallel
PURGE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --run-id) RUN_ID="$2"; shift ;;
        --seed) SEED="$2"; shift ;;
        --requirement-file) REQUIREMENT="$2"; shift ;;
        --rounds) ROUNDS="$2"; shift ;;
        --platform) PLATFORM="$2"; shift ;;
        --purge) PURGE=1 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

umask 077
STATE="$OPS_DIR/state"
RUN_DIR="$OPS_DIR/runs/$RUN_ID"
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ && "$RUN_ID" != *..* ]] || { echo "invalid run id" >&2; exit 2; }
command -v lsof >/dev/null 2>&1 || { echo "FAIL: lsof is required" >&2; exit 1; }
[[ ! -e "$RUN_DIR/manifest.json" ]] || {
    echo "FAIL: run $RUN_ID already exists; a resumed run would skip the stages being audited" >&2; exit 1; }
for pidfile in "$STATE/backend.pid" "$STATE/frontend.pid"; do
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "FAIL: MiroFish is already running without auditing; run ops/scripts/stop.sh first" >&2
        exit 1
    fi
done
make_private_dir "$OPS_DIR/runs"
make_private_dir "$RUN_DIR"
AUDIT="$RUN_DIR/netaudit.jsonl"
LSOF="$RUN_DIR/lsof.jsonl"
: >"$LSOF"

sampler_pid=""
cleanup() {
    [[ -n "$sampler_pid" ]] && kill -TERM "$sampler_pid" 2>/dev/null && wait "$sampler_pid" 2>/dev/null
    "$OPS_DIR/scripts/stop.sh" || true
}
trap cleanup EXIT

"$OPS_DIR/scripts/start.sh" --netaudit "$AUDIT"

python3 "$OPS_DIR/scripts/netcheck.py" sample --out "$LSOF" \
    --pidfile "$STATE/backend.pid" --pidfile "$STATE/frontend.pid" --pidfile "$STATE/meter.pid" \
    --ollama-port "${OLLAMA_HOST##*:}" --interval 1 &
sampler_pid=$!
sleep 2
kill -0 "$sampler_pid" 2>/dev/null || { echo "FAIL: the lsof sampler did not start" >&2; exit 1; }

echo "== Pilot run $RUN_ID: $ROUNDS rounds on $PLATFORM =="
pilot_status=0
python3 "$OPS_DIR/scripts/mirofish_api.py" run --run-id "$RUN_ID" --seed "$SEED" \
    --requirement-file "$REQUIREMENT" --max-rounds "$ROUNDS" --platform "$PLATFORM" || pilot_status=$?

sampler_alive=1
kill -0 "$sampler_pid" 2>/dev/null || sampler_alive=0
cleanup
trap - EXIT
sampler_pid=""
if [[ "$sampler_alive" -eq 0 ]]; then
    echo "FAIL: the lsof sampler stopped during the pilot; socket coverage is incomplete" >&2
    exit 1
fi

echo "== Network evaluation =="
check_status=0
python3 "$OPS_DIR/scripts/netcheck.py" evaluate --audit "$AUDIT" --lsof "$LSOF" --expect-simulation \
    --allowed "$OPS_DIR/config/allowed_hosts.txt" --report "$RUN_DIR/netcheck.json" || check_status=$?

if [[ "$PURGE" -eq 1 ]]; then
    "$OPS_DIR/scripts/purge.sh" "$RUN_ID" --yes
fi
if [[ "$check_status" -eq 0 && "$pilot_status" -ne 0 ]]; then
    echo "INCONCLUSIVE: no violations, but the pilot failed (exit $pilot_status), so later stages were not observed" >&2
    exit 2
fi
exit "$check_status"
