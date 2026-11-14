#!/usr/bin/env bash
# One-command setup on a new machine (macOS or Linux). Idempotent: safe to re-run.
#
#   1. clone upstream MiroFish into ./mirofish at the commit pinned in ops/UPSTREAM.md
#   2. apply ops/patches
#   3. install backend deps from uv.lock (uv sync --frozen) and frontend deps from
#      package-lock.json (npm ci --ignore-scripts); upstream's own setup scripts are not run
#   4. create mirofish/.env (mode 600) from ops/config/mirofish.env.example with a random SECRET_KEY,
#      or add keys that a newer example introduced to an existing .env (values are kept)
#   5. create private (0700) data directories
#   6. cache the simulation's runtime downloads (twhin-bert, tiktoken; ~1.1 GB) and switch .env offline
#   7. run the backend and ops test suites
#   8. check Ollama and the models; with --with-models, start Ollama and pull them (~19 GB)
#
# Usage: ops/scripts/bootstrap.sh [--with-models] [--no-frontend] [--skip-tests] [--skip-assets]
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$OPS_DIR/scripts/_common.sh"

WITH_MODELS=0
WITH_FRONTEND=1
RUN_TESTS=1
CACHE_ASSETS=1
for arg in "$@"; do
    case "$arg" in
        --with-models) WITH_MODELS=1 ;;
        --no-frontend) WITH_FRONTEND=0 ;;
        --skip-tests) RUN_TESTS=0 ;;
        --skip-assets) CACHE_ASSETS=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

UPSTREAM_URL="https://github.com/666ghj/MiroFish.git"
UPSTREAM_DIR="$PROJECT_ROOT/mirofish"
PINNED_COMMIT="$(grep -oE '^\| Pinned commit \| `[0-9a-f]{40}`' "$OPS_DIR/UPSTREAM.md" | grep -oE '[0-9a-f]{40}')"
# Upstream requires Python >=3.11,<3.13; override with UV_PYTHON=3.11 if preferred.
export UV_PYTHON="${UV_PYTHON:-3.12}"

step() { printf '\n== %s ==\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is required: $2"; }

step "Prerequisites"
need git "install Git"
need uv "install uv: https://docs.astral.sh/uv/getting-started/installation/"
need python3 "install Python 3 (used for small helpers)"
if [[ $WITH_FRONTEND -eq 1 ]]; then
    need node "install Node.js 18 or newer (or pass --no-frontend)"
    need npm "install npm (ships with Node.js)"
    node_major="$(node -p 'process.versions.node.split(".")[0]')"
    [[ "$node_major" -ge 18 ]] || fail "Node.js 18+ required, found $(node --version)"
fi
echo "git $(git --version | awk '{print $3}'), uv $(uv --version | awk '{print $2}'), Python target $UV_PYTHON, node $(node --version 2>/dev/null || echo n/a)"

step "Upstream MiroFish at $PINNED_COMMIT"
if [[ ! -e "$UPSTREAM_DIR" ]]; then
    git clone --no-checkout "$UPSTREAM_URL" "$UPSTREAM_DIR"
    git -C "$UPSTREAM_DIR" checkout --quiet --detach "$PINNED_COMMIT"
elif [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
    fail "$UPSTREAM_DIR exists but is not a git checkout; move it aside and re-run"
fi
head_commit="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
[[ "$head_commit" == "$PINNED_COMMIT" ]] || fail "mirofish is at $head_commit, expected $PINNED_COMMIT"
echo "OK: mirofish at pinned commit"

step "Patches"
"$OPS_DIR/scripts/apply_patches.sh"

step "Backend dependencies (uv sync --frozen)"
(cd "$UPSTREAM_DIR/backend" && uv sync --frozen)

if [[ $WITH_FRONTEND -eq 1 ]]; then
    step "Frontend dependencies (npm ci --ignore-scripts)"
    # Only esbuild and fsevents declare install scripts; both work from their prebuilt
    # platform packages, so no third-party install code needs to run.
    (cd "$UPSTREAM_DIR/frontend" && npm ci --ignore-scripts --no-audit --no-fund)
    (cd "$UPSTREAM_DIR/frontend" && node node_modules/esbuild/bin/esbuild --version >/dev/null) \
        || fail "esbuild platform binary missing after npm ci"
    echo "OK: frontend dependencies installed"
fi

step "Configuration (mirofish/.env)"
ENV_FILE="$UPSTREAM_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    (umask 077 && sed "s/^SECRET_KEY=change-me$/SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')/" \
        "$OPS_DIR/config/mirofish.env.example" > "$ENV_FILE")
    echo "created $ENV_FILE (mode 600, random SECRET_KEY)"
else
    echo "kept existing $ENV_FILE ($(python3 "$OPS_DIR/scripts/envfile.py" sync "$OPS_DIR/config/mirofish.env.example" "$ENV_FILE"))"
    # Older .env files point straight at Ollama; route through the LLM meter (Phase 4).
    if [[ "$(python3 "$OPS_DIR/scripts/envfile.py" get "$ENV_FILE" LLM_BASE_URL 2>/dev/null)" == "http://127.0.0.1:11434/v1" ]]; then
        python3 "$OPS_DIR/scripts/envfile.py" set "$ENV_FILE" "LLM_BASE_URL=http://127.0.0.1:${LLM_METER_PORT:-11435}/v1"
        echo "LLM_BASE_URL now goes through the LLM meter on port ${LLM_METER_PORT:-11435}"
    fi
fi
chmod 600 "$ENV_FILE"
grep -q '^SECRET_KEY=change-me$' "$ENV_FILE" && echo "WARN: SECRET_KEY in $ENV_FILE is still the placeholder"
grep -q '^MIROFISH_MEMORY_BACKEND=local$' "$ENV_FILE" || echo "WARN: MIROFISH_MEMORY_BACKEND=local is not set in $ENV_FILE"
if grep -qE '^ZEP_API_KEY=.+' "$ENV_FILE"; then
    echo "WARN: ZEP_API_KEY is set in $ENV_FILE; seed content must not go to Zep Cloud"
fi

step "Private data directories (0700)"
for dir in "$UPSTREAM_DIR/backend/uploads" "$UPSTREAM_DIR/backend/logs" "$OPS_DIR/logs" "$OPS_DIR/state" \
           "$OPS_DIR/seeds" "$OPS_DIR/outputs" "$OPS_DIR/runs" "$OPS_DIR/cache" "$OPS_DIR/mappings"; do
    make_private_dir "$dir"
    echo "$dir"
done

if [[ $CACHE_ASSETS -eq 1 ]]; then
    step "Runtime assets for offline simulation"
    "$OPS_DIR/scripts/cache_assets.sh"
else
    echo "Skipped asset caching; the Twitter simulation will try to reach huggingface.co until cache_assets.sh is run."
fi

if [[ $RUN_TESTS -eq 1 ]]; then
    step "Backend tests"
    (cd "$UPSTREAM_DIR/backend" && .venv/bin/python -m pytest -q -p no:cacheprovider -W ignore::SyntaxWarning)
    step "Ops tests"
    # The backend interpreter has PyYAML (pinned in upstream's uv.lock), which the Phase 4 scripts use.
    "$UPSTREAM_DIR/backend/.venv/bin/python" -m unittest discover -s "$OPS_DIR/tests"
fi

step "Local LLM (Ollama)"
OLLAMA="$(resolve_ollama_bin)"
if [[ -z "$OLLAMA" ]]; then
    echo "Ollama not found. Install it from https://ollama.com/download (or put the binary in"
    echo "  .tools/ollama/current/ollama, or set OLLAMA_BIN in ops/config/model.conf), then re-run with --with-models."
    exit 0
fi
echo "ollama: $OLLAMA"
if [[ $WITH_MODELS -eq 1 ]]; then
    "$OPS_DIR/scripts/ollama_serve.sh"
    "$OPS_DIR/scripts/build_model.sh"
    "$OPS_DIR/scripts/doctor.sh"
elif curl --noproxy '*' -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
    "$OPS_DIR/scripts/doctor.sh" || echo "Models missing? Re-run with --with-models (downloads ~19 GB)."
else
    echo "Ollama is installed but not running. Next: ops/scripts/bootstrap.sh --with-models"
fi

step "Done"
echo "Setup complete. Start with ops/scripts/start.sh, stop with ops/scripts/stop.sh (see ops/README.md)."
