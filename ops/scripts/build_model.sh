#!/usr/bin/env bash
# Pull the configured Hugging Face GGUFs through Ollama and create the derived models:
#   OLLAMA_MODEL_ALIAS  chat model with the context window from ops/config/model.conf
#   EMBED_MODEL_ALIAS   embedding model for the local memory-layer adapter
# Usage: ops/scripts/build_model.sh [--chat-only|--embed-only]
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=_common.sh
source "$OPS_DIR/scripts/_common.sh"

bin="$(resolve_ollama_bin)"
if [[ -z "$bin" ]]; then
    echo "ERROR: no Ollama binary found (see ops/README.md)." >&2
    exit 1
fi

HF_REF_PATTERN='^hf\.co/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?$'
ALIAS_PATTERN='^[a-z0-9][a-z0-9._-]*$'

# create_alias <hf_ref> <alias> [num_ctx]
create_alias() {
    local ref="$1" alias="$2" num_ctx="${3:-}"
    if ! [[ "$ref" =~ $HF_REF_PATTERN ]]; then
        echo "ERROR: model ref must look like hf.co/<org>/<repo>:<quant> (got '$ref')." >&2
        exit 1
    fi
    if ! [[ "$alias" =~ $ALIAS_PATTERN ]]; then
        echo "ERROR: invalid alias '$alias'." >&2
        exit 1
    fi

    echo "Pulling $ref ..."
    "$bin" pull "$ref"

    local modelfile
    modelfile="$(mktemp "${TMPDIR:-/tmp}/mirofish-modelfile.XXXXXX")"
    if [[ -n "$num_ctx" ]]; then
        sed -e "s|__HF_MODEL_REF__|$ref|" -e "s|__NUM_CTX__|$num_ctx|" \
            "$OPS_DIR/config/Modelfile.template" > "$modelfile"
    else
        printf 'FROM %s\n' "$ref" > "$modelfile"
    fi
    echo "Creating $alias ${num_ctx:+(num_ctx=$num_ctx) }..."
    "$bin" create "$alias" -f "$modelfile"
    rm -f "$modelfile"
}

mode="${1:-all}"
if [[ "$mode" != "--embed-only" ]]; then
    if ! [[ "$NUM_CTX" =~ ^[0-9]+$ ]] || (( NUM_CTX < 16384 )); then
        echo "ERROR: NUM_CTX must be an integer >= 16384 (got '$NUM_CTX')." >&2
        exit 1
    fi
    create_alias "$HF_MODEL_REF" "$OLLAMA_MODEL_ALIAS" "$NUM_CTX"
fi
if [[ "$mode" != "--chat-only" ]]; then
    create_alias "$EMBED_MODEL_REF" "$EMBED_MODEL_ALIAS"
fi
echo "Done. Run ops/scripts/doctor.sh to verify."
