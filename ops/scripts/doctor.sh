#!/usr/bin/env bash
# Check the local LLM setup: Ollama installed, reachable, loopback-only, memory budget,
# model present with a large context, structured-JSON compliance, and throughput.
# Writes the hardware profile used by plan.py to ops/state/hardware_profile.json.
#
# Usage: ops/scripts/doctor.sh [--skip-bench]
set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=_common.sh
source "$OPS_DIR/scripts/_common.sh"
PROFILE_OUT="$OPS_DIR/state/hardware_profile.json"
PYTHON="${PYTHON:-python3}"
EXTRA_ARGS=("$@")

failures=0
pass() { echo "PASS  $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; failures=$((failures + 1)); }
info() { echo "INFO  $*"; }

echo "== MiroFish local LLM doctor =="
info "model ref: $HF_MODEL_REF -> alias: $OLLAMA_MODEL_ALIAS (num_ctx $NUM_CTX)"

ollama_port="${OLLAMA_HOST##*:}"
ollama_addr="${OLLAMA_HOST%:*}"
if [[ "$ollama_addr" != "127.0.0.1" && "$ollama_addr" != "localhost" ]]; then
    fail "OLLAMA_HOST in model.conf is '$OLLAMA_HOST'; it must be 127.0.0.1:<port>"
fi

# --- 1. Ollama binary ---
ollama_bin="$(resolve_ollama_bin)"
if [[ -n "$ollama_bin" ]]; then
    pass "ollama binary: $ollama_bin ($("$ollama_bin" --version 2>/dev/null | tail -1))"
else
    fail "no Ollama binary found (project .tools/ollama/current or PATH)"
fi

# --- 2. Reachability ---
if version_json="$(curl --noproxy '*' -fsS --max-time 5 "http://$OLLAMA_HOST/api/version" 2>/dev/null)"; then
    pass "Ollama reachable at http://$OLLAMA_HOST ($version_json)"
    reachable=1
else
    fail "Ollama not reachable at http://$OLLAMA_HOST (is 'ollama serve' or the Ollama app running?)"
    reachable=0
fi

# --- 3. Loopback-only listener ---
if [[ $reachable -eq 1 ]]; then
    if command -v lsof >/dev/null 2>&1; then
        listeners="$(lsof -nP -iTCP:"$ollama_port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9}' | sort -u)"
    elif command -v ss >/dev/null 2>&1; then
        listeners="$(ss -ltnH "sport = :$ollama_port" 2>/dev/null | awk '{print $4}' | sort -u)"
    else
        listeners=""
    fi
    if [[ -z "$listeners" ]]; then
        warn "could not list listeners on port $ollama_port (permissions?); bind not verified"
    elif echo "$listeners" | grep -Evq '^(127\.0\.0\.1|\[::1\]|localhost):'"$ollama_port"'$'; then
        fail "Ollama listens on a non-loopback address: $(echo "$listeners" | tr '\n' ' ')- set OLLAMA_HOST=127.0.0.1:$ollama_port"
    else
        pass "Ollama bound to loopback only: $(echo "$listeners" | tr '\n' ' ')"
    fi
fi

# --- 4. Memory budget ---
total_gb=0
budget_gb=0
case "$(uname -s)" in
    Darwin)
        mem_bytes="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
        total_gb=$((mem_bytes / 1024 / 1024 / 1024))
        wired_mb="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)"
        if [[ "$wired_mb" =~ ^[0-9]+$ ]] && (( wired_mb > 0 )); then
            budget_gb=$((wired_mb / 1024))
            info "unified memory ${total_gb} GB; GPU wired limit set to ${budget_gb} GB"
        else
            # macOS default GPU working-set limit is roughly 75% of RAM on large machines
            # and about 2/3 on smaller ones. Approximation, not an exact API value.
            if (( total_gb >= 64 )); then budget_gb=$((total_gb * 3 / 4)); else budget_gb=$((total_gb * 2 / 3)); fi
            info "unified memory ${total_gb} GB; GPU-usable memory approx ${budget_gb} GB (macOS default limit)"
        fi
        ;;
    Linux)
        mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
        total_gb=$((mem_kb / 1024 / 1024))
        if command -v nvidia-smi >/dev/null 2>&1; then
            vram_mb="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1)"
            budget_gb=$(( ${vram_mb:-0} / 1024 ))
            info "RAM ${total_gb} GB; largest GPU VRAM ${budget_gb} GB"
        else
            warn "no nvidia-smi found; assuming CPU-only inference (slow)"
        fi
        ;;
    *)
        warn "unsupported OS for memory detection: $(uname -s)"
        ;;
esac
if (( total_gb == 0 )); then
    warn "could not read total memory (sysctl or /proc blocked?); tier advice below is unreliable"
fi
if (( budget_gb >= 24 )); then
    tier="27-32B class"
elif (( budget_gb >= 16 )); then
    tier="14B class"
elif (( budget_gb >= 8 )); then
    tier="8B class"
else
    tier="below 8 GB: expect very slow or failing runs"
fi
info "recommended model tier for this machine: $tier (Q4_K_M)"

# --- 5-7. Model, context, JSON compliance, benchmark ---
if [[ $reachable -eq 1 ]]; then
    "$PYTHON" "$OPS_DIR/scripts/doctor_llm.py" \
        --host "$OLLAMA_HOST" \
        --model "$OLLAMA_MODEL_ALIAS" \
        --min-pass "$JSON_MIN_PASS" \
        --memory-budget-gb "$budget_gb" \
        --total-memory-gb "$total_gb" \
        --profile-out "$PROFILE_OUT" \
        "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
    llm_status=$?
    if (( llm_status != 0 )); then failures=$((failures + 1)); fi

    embed_dim="$(curl --noproxy '*' -fsS --max-time 120 "http://$OLLAMA_HOST/api/embed" \
        -d "{\"model\": \"$EMBED_MODEL_ALIAS\", \"input\": \"doctor check\"}" 2>/dev/null \
        | "$PYTHON" -c 'import json,sys; print(len(json.load(sys.stdin)["embeddings"][0]))' 2>/dev/null)"
    if [[ "$embed_dim" =~ ^[0-9]+$ ]] && (( embed_dim > 0 )); then
        pass "embedding model $EMBED_MODEL_ALIAS returns ${embed_dim}-dim vectors"
    else
        fail "embedding model $EMBED_MODEL_ALIAS unavailable (run ops/scripts/build_model.sh --embed-only)"
    fi
else
    fail "skipping model, JSON and benchmark checks (Ollama unreachable)"
fi

echo
if (( failures == 0 )); then
    echo "doctor: ALL CHECKS PASSED"
    exit 0
fi
echo "doctor: $failures check group(s) FAILED"
exit 1
