#!/bin/bash
# Validate the current HA configuration before restarting its container.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/ha_safe_reload.py" "$@"
