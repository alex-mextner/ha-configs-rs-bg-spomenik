#!/usr/bin/env bash
# Standalone entry for Debian/Ubuntu hosts that have no debian-tweaks machine.toml of their own (ubuntu-pc,
# odroidn2 while it still runs Armbian). bananapi-m4 runs this module through its own install.sh instead.
#   ./install.sh HOST            preview (plan): every change as a diff; changes nothing; exit 3 while drifting
#   ./install.sh HOST --commit   apply; a second run is silent and exits 0
#   ./install.sh HOST check      read-only, exit 0 converged / 3 drift
# HOST is a file name in ../hosts/ without .toml. scope = "system" hosts need sudo (without it: exit 6); scope = "user"
# hosts run as the user.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ $# -ge 1 ]] || { sed -n '2,8p' "$0" >&2; exit 2; }
host=$1
case ${2:-} in
"") mode=plan ;;
--commit) mode=install ;;
check) mode=check ;;
*) sed -n '2,8p' "$0" >&2; exit 2 ;;
esac
for tool in python3 jq curl; do
	command -v "$tool" >/dev/null || { echo "install.sh: $tool is missing: apt-get install -y $tool" >&2; exit 2; }
done
# the debian-tweaks module helpers (lib/common.sh) live with the first debian-tweaks machine
DT_DIR=$(cd "$here/../../bananapi-m4" && pwd)
DT_OPTIONS=$(jq -cn --arg host "$host" '{host: $host}')
export DT_DIR DT_OPTIONS
code=0
"$here/run" "$mode" || code=$?
if [[ $mode == plan && $code == 3 ]]; then echo "preview only: run \`$0 $host --commit\` to apply"; fi
exit "$code"
