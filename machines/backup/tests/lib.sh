# shellcheck shell=bash
# Shared by the machines/backup tests: a scratch dir, pass/fail, and the pinned restic + rclone the tests run with.
#   source "$here/lib.sh"      sets backup_dir, bin (the pinned binaries' dir) and tmp; PATH starts with $bin
backup_dir=$(dirname "${here:?set by the test that sources this file}")
# where debian/run installs the pinned binaries (tests/in-docker.sh mounts them at /opt/bin)
bin=${HOMELAB_BACKUP_TEST_BIN:-$HOME/.local/bin}
PATH=$bin:$PATH

pinned_version() {
	python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))[sys.argv[2]]["version"])' \
		"$backup_dir/policy.toml" "$1"
}
[[ $("$bin/restic" version 2>/dev/null | cut -d' ' -f1-2) == "restic $(pinned_version restic)" &&
	$("$bin/rclone" version 2>/dev/null | head -1) == "rclone v$(pinned_version rclone)" ]] || {
	echo "$(basename "$0"): needs the pinned restic $(pinned_version restic) and rclone $(pinned_version rclone) in $bin" \
		"(machines/backup/debian/install.sh puts them there)" >&2
	exit 2
}

tmp=$(mktemp -d "/var/tmp/homelab-backup-$(basename "$0" .sh).XXXXXX")
# everything the tests start (restic, sqlite3, debian/run's mktemp) keeps its temp files in the scratch dir too
export TMPDIR=$tmp/tmp
mkdir "$TMPDIR"
pass() { echo "ok - $*"; }
fail() {
	echo "FAIL - $*" >&2
	echo "kept for inspection: $tmp" >&2
	exit 1
}
trap 'echo "FAIL - line $LINENO: $BASH_COMMAND (kept for inspection: $tmp)" >&2' ERR
