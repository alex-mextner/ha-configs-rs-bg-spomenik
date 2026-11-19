#!/usr/bin/env bash
# The one entry point: converge this machine to machine.toml.
#   sudo ./install.sh              preview: every change, as diffs; changes nothing (exit 3 while something drifts)
#   sudo ./install.sh --commit     apply; re-running is safe and ends in "N ok" once converged
#   sudo ./install.sh check        read-only, exit 0 converged / 3 drift (cron, monitoring); 6 without root
#   ./install.sh list              modules, order, on/off
# Every command but `list` exits 2 when `hostname` is not [modules.system] hostname of machine.toml (a file copied to
# the wrong box); `--force-host <current hostname>` overrides it, e.g. on a fresh board before the `system` module
# renames it (a different name still stops: the wrong box picked by its address).
# Needs python3 (3.11+, tomllib) and jq for the engine, gpg and curl for apt-sources (which runs before `packages`
# could install them). Engine: lib/engine.py.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
for tool in python3 jq gpg curl; do
	if ! command -v "$tool" >/dev/null; then
		echo "install.sh: $tool is missing: apt-get install -y $tool" >&2
		exit 2
	fi
done
exec python3 -B "$here/lib/engine.py" "$@"
