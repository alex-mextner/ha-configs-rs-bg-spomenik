#!/usr/bin/env bash
# The engine exits 130 / 143 for a SIGINT / SIGTERM that arrives after the last module has finished (it stops the
# run, it is not swallowed). The engine is imported, its module run replaced by one that only gets the signal.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

machine=$WORK/machine.toml
printf '[modules.system]\nenabled = false\nhostname = "%s"\n' "$(hostname)" >"$machine"
for signum in 2 15; do
	run python3 -B - "$MACHINES/bananapi-m4/lib" "$machine" "$signum" <<'EOF'
import os, signal, sys

sys.path.insert(0, sys.argv[1])
import engine

machine, signum = sys.argv[2], int(sys.argv[3])
os.geteuid = lambda: 0


def last_module_then_signal(*_args):
    os.kill(os.getpid(), signum)
    return {}


engine.run_selected = last_module_then_signal
sys.argv = ["engine.py", "--machine", machine, "check"]
sys.exit(engine.main())
EOF
	expect "a signal $signum after the last module: exit $((128 + signum))" test "$code" = $((128 + signum))
	expect "  ... saying the run stopped" contains "$err" "stopped on"
done

done_testing
