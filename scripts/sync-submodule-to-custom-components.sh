#!/usr/bin/env bash
# Pinned, offline deployment from this checkout; preserves patches and tracked links.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET=()
if [[ $# -gt 0 && "$1" != -* ]]; then
    TARGET=(--target "$1")
    shift
fi
exec "${HA_PYTHON:-python3}" "$SCRIPT_DIR/ha_setup_integrations.py" "${TARGET[@]}" "$@"
