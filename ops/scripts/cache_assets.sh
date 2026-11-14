#!/usr/bin/env bash
# Download, once, the files MiroFish's simulation otherwise fetches at runtime, then switch
# the backend to offline mode so it never contacts huggingface.co or openaipublic again:
#   - Twitter/twhin-bert-base (OASIS Twitter recommender; ~1.1 GB) -> ops/cache/huggingface
#   - tiktoken cl100k_base and o200k_base encodings                -> ops/cache/tiktoken
# Writes HF_HOME, TIKTOKEN_CACHE_DIR, HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 to mirofish/.env
# and records what was cached in ops/state/assets.json. Idempotent.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$OPS_DIR/scripts/_common.sh"

PYTHON="$PROJECT_ROOT/mirofish/backend/.venv/bin/python"
ENV_FILE="$PROJECT_ROOT/mirofish/.env"
[[ -x "$PYTHON" ]] || { echo "FAIL: backend venv missing; run ops/scripts/bootstrap.sh first" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "FAIL: $ENV_FILE missing; run ops/scripts/bootstrap.sh first" >&2; exit 1; }

make_private_dir "$OPS_DIR/cache"
make_private_dir "$OPS_DIR/cache/huggingface"
make_private_dir "$OPS_DIR/cache/tiktoken"
make_private_dir "$OPS_DIR/state"
export HF_HOME="$OPS_DIR/cache/huggingface"
export TIKTOKEN_CACHE_DIR="$OPS_DIR/cache/tiktoken"
export HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1

run_python() {
    "$PYTHON" -W ignore - "$@" <<'EOF'
import json, os, sys
from pathlib import Path

import tiktoken
from transformers import AutoModel, AutoTokenizer

MODEL = "Twitter/twhin-bert-base"
AutoTokenizer.from_pretrained(MODEL, model_max_length=512)
AutoModel.from_pretrained(MODEL)
encodings = {name: tiktoken.get_encoding(name).n_vocab for name in ("cl100k_base", "o200k_base")}

ref = Path(os.environ["HF_HOME"], "hub", "models--Twitter--twhin-bert-base", "refs", "main")
info = {
    "twhin_bert_revision": ref.read_text().strip() if ref.exists() else None,
    "tiktoken_encodings": encodings,
}
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(info, indent=2) + "\n")
print(json.dumps(info))
EOF
}

echo "== Downloading (setup-time network access: huggingface.co, openaipublic.blob.core.windows.net) =="
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 run_python "$OPS_DIR/state/assets.json"

echo "== Verifying the cache works with networking disabled (with ops/runtime shims, as start.sh runs it) =="
PYTHONPATH="$OPS_DIR/runtime${PYTHONPATH:+:$PYTHONPATH}" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 run_python >/dev/null
echo "OK: assets load offline"

python3 "$OPS_DIR/scripts/envfile.py" set "$ENV_FILE" \
    "HF_HOME=$HF_HOME" "TIKTOKEN_CACHE_DIR=$TIKTOKEN_CACHE_DIR" \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
echo "OK: $ENV_FILE now runs MiroFish offline (HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1)"
