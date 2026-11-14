#!/usr/bin/env bash
# Derive ops/docker/mirofish.docker.env (mode 600, git-ignored) from mirofish/.env for
# ops/docker/compose.local.yml. Only the values that differ inside the container change:
#   FLASK_HOST=0.0.0.0   so the gateway can reach the backend; the namespace it binds in is
#                        attached only to the internal network, and the host sees 127.0.0.1 only
#   HF_HOME / TIKTOKEN_CACHE_DIR point at the read-only ops/cache mount
#   LLM_BASE_URL goes straight to the Ollama sidecar (the host-side llm_meter.py is not in the container)
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$OPS_DIR/../mirofish/.env"
DST="$OPS_DIR/docker/mirofish.docker.env"
[[ -f "$SRC" ]] || { echo "FAIL: $SRC missing; run ops/scripts/bootstrap.sh first" >&2; exit 1; }
umask 077
cp "$SRC" "$DST"
chmod 600 "$DST"
python3 "$OPS_DIR/scripts/envfile.py" set "$DST" \
    FLASK_HOST=0.0.0.0 \
    LLM_BASE_URL=http://127.0.0.1:11434/v1 \
    HF_HOME=/app/ops-cache/huggingface \
    TIKTOKEN_CACHE_DIR=/app/ops-cache/tiktoken
echo "wrote $DST"
