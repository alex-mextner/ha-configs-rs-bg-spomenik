#!/bin/bash
set -euo pipefail
export HA_DESKTOP_PARENT_NETNS
HA_DESKTOP_PARENT_NETNS=$(readlink /proc/self/ns/net)
exec /usr/bin/bwrap --unshare-net --bind / / --dev-bind /dev /dev --die-with-parent -- /usr/bin/python3 "$HOME/.local/share/ha-physical-desktop/session.py"
