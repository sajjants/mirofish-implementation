#!/usr/bin/env bash
# Apply ops/patches/*.patch to ./mirofish in filename order. Idempotent: patches that are
# already applied are skipped. Refuses to run if ./mirofish is not at the pinned commit.
#
# Usage: ops/scripts/apply_patches.sh [--check | --revert]
#   --check   report the state of each patch without changing anything
#   --revert  reverse-apply the patches (newest first), returning to pristine upstream code
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$OPS_DIR/scripts/_common.sh"

MODE="apply"
case "${1:-}" in
    "") ;;
    --check) MODE="check" ;;
    --revert) MODE="revert" ;;
    *) echo "usage: $0 [--check | --revert]" >&2; exit 2 ;;
esac

UPSTREAM_DIR="$PROJECT_ROOT/mirofish"
PINNED_COMMIT="$(grep -oE '^\| Pinned commit \| `[0-9a-f]{40}`' "$OPS_DIR/UPSTREAM.md" | grep -oE '[0-9a-f]{40}')"

if [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
    echo "FAIL: $UPSTREAM_DIR is not a git checkout (see ops/UPSTREAM.md)" >&2
    exit 1
fi
HEAD_COMMIT="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
if [[ "$HEAD_COMMIT" != "$PINNED_COMMIT" ]]; then
    echo "FAIL: mirofish is at $HEAD_COMMIT, expected pinned $PINNED_COMMIT" >&2
    exit 1
fi

shopt -s nullglob
PATCHES=("$OPS_DIR"/patches/*.patch)
if [[ ${#PATCHES[@]} -eq 0 ]]; then
    echo "No patches in ops/patches."
    exit 0
fi
if [[ "$MODE" == "revert" ]]; then
    REVERSED=()
    for ((i = ${#PATCHES[@]} - 1; i >= 0; i--)); do REVERSED+=("${PATCHES[i]}"); done
    PATCHES=("${REVERSED[@]}")
fi

is_applied() { git -C "$UPSTREAM_DIR" apply --reverse --check "$1" >/dev/null 2>&1; }
can_apply() { git -C "$UPSTREAM_DIR" apply --check "$1" >/dev/null 2>&1; }

status=0
for patch in "${PATCHES[@]}"; do
    name="$(basename "$patch")"
    if is_applied "$patch"; then
        case "$MODE" in
            revert) git -C "$UPSTREAM_DIR" apply --reverse "$patch"; echo "REVERTED  $name" ;;
            *) echo "APPLIED   $name (already)" ;;
        esac
    elif can_apply "$patch"; then
        case "$MODE" in
            apply) git -C "$UPSTREAM_DIR" apply "$patch"; echo "APPLIED   $name" ;;
            check) echo "PENDING   $name (applies cleanly)" ;;
            revert) echo "SKIPPED   $name (not applied)" ;;
        esac
    else
        echo "CONFLICT  $name: neither applies nor reverses cleanly; inspect git -C mirofish status" >&2
        status=1
        [[ "$MODE" == "check" ]] || break
    fi
done
exit "$status"
