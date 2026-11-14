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

# Print why the project directory looks cloud-synced (seeds and runs would be uploaded), or nothing.
# Heuristic: known sync locations and marker files in the real path or any of its parents.
cloud_sync_reason() {
    local dir
    dir="$(cd -P "$PROJECT_ROOT" && pwd)"
    case "$dir/" in
        */Library/CloudStorage/*) echo "it is under ~/Library/CloudStorage (Google Drive, OneDrive or Dropbox)"; return ;;
        */Library/Mobile\ Documents/*) echo "it is in iCloud Drive"; return ;;
    esac
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        if [[ -e "$dir/.tmp.driveupload" ]]; then
            echo "Google Drive is backing up $dir (found $dir/.tmp.driveupload)"; return
        fi
        if [[ -e "$dir/.dropbox" || -e "$dir/.dropbox.cache" ]]; then
            echo "Dropbox is syncing $dir"; return
        fi
        dir="$(dirname "$dir")"
    done
}
