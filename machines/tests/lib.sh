# shellcheck shell=bash disable=SC2034 # MACHINES, code, out, err: read by the test files that source this one
# Helpers for machines/tests/test-*.sh (sourced; run.sh runs every test file in a shell of its own).
set -euo pipefail

MACHINES=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
FAILURES=0
# set by run
code=0 out='' err=''

# run CMD...: run CMD, keeping its exit code in $code, stdout in $out and stderr in $err
run() {
	code=0
	"$@" >"$WORK/.out" 2>"$WORK/.err" || code=$?
	out=$(<"$WORK/.out")
	err=$(<"$WORK/.err")
}

pass() { echo "ok   $*"; }
fail() {
	echo "FAIL $*"
	[[ -z ${out:-} ]] || sed 's/^/     out: /' <<<"$out"
	[[ -z ${err:-} ]] || sed 's/^/     err: /' <<<"$err"
	FAILURES=$((FAILURES + 1))
}

# expect NAME CONDITION...: pass when the condition (a command) succeeds
expect() {
	local name=$1
	shift
	if "$@"; then pass "$name"; else fail "$name"; fi
}

contains() { [[ $1 == *"$2"* ]]; }

lacks() { [[ $1 != *"$2"* ]]; }

done_testing() {
	((FAILURES == 0)) || { echo "$FAILURES failure(s)"; exit 1; }
}
