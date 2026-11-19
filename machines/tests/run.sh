#!/usr/bin/env bash
# Shell tests of machines/: every test-*.sh next to this file, each in a shell of its own. Needs bash, git, python3
# (3.11+), jq, gpg and curl (install.sh checks for them); no root, no network. `make test-machines` runs it in a
# debian:trixie container.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
failed=()
for test in test-*.sh; do
	echo "== $test"
	bash "$test" || failed+=("$test")
done
if ((${#failed[@]})); then
	echo "failed: ${failed[*]}"
	exit 1
fi
echo "all passed"
