#!/usr/bin/env bash
# Public update.install API keeps HACS reconciliation inside HA's installation flow.
set -euo pipefail
if [[ $# -eq 0 || "$1" == --help || "$1" == -h ]]; then
    echo 'Usage: apply-fork-update.sh update.<entity> [--version <exact-release>] [--url <HA URL>] [--env-file <file>]'
    echo 'Use ha_updates.py registry/repositories/states to identify the entity and release.'
    [[ $# -gt 0 ]] && exit 0
    exit 2
fi
ENTITY="$1"
shift
if [[ "$ENTITY" != update.* ]]; then
    echo 'An explicit update.* entity is required; submodule/fork/branch arguments are unsupported.' >&2
    exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${HA_PYTHON:-python3}" "$SCRIPT_DIR/ha_updates.py" install --entity "$ENTITY" "$@"
