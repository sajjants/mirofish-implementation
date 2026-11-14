# Shared helpers for ops/scripts/*.sh. Source after setting OPS_DIR.
# shellcheck shell=bash

PROJECT_ROOT="$(cd "$OPS_DIR/.." && pwd)"
# shellcheck source=../config/model.conf
source "$OPS_DIR/config/model.conf"
export OLLAMA_HOST

# Print the Ollama binary to use, or nothing if none is available.
resolve_ollama_bin() {
    if [[ -n "${OLLAMA_BIN:-}" ]]; then
        echo "$OLLAMA_BIN"
    elif [[ -x "$PROJECT_ROOT/.tools/ollama/current/ollama" ]]; then
        echo "$PROJECT_ROOT/.tools/ollama/current/ollama"
    elif command -v ollama >/dev/null 2>&1; then
        command -v ollama
    fi
}

# Create a directory (and parents) readable only by the current user.
make_private_dir() {
    mkdir -p "$1"
    chmod 700 "$1"
}
