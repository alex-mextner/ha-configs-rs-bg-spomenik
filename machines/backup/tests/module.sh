#!/usr/bin/env bash
# The debian-tweaks module (debian/run, through debian/install.sh) converges and then stays put: plan on a fresh
# system shows drift (exit 3), --commit converges and links homelab-backup onto PATH, check and plan are clean, a
# second --commit writes nothing, and units of an optional verb the host file no longer asks for are disabled and
# removed. It refuses: a scope = "system" host without root (exit 6), a host file without paths (exit 2), and a
# foreign file where the link goes (exit 1, before anything is written).
# As a normal user: scope = "user" (hosts/ubuntu-pc.toml) in a scratch $HOME. As root only inside the throwaway
# container of tests/in-docker.sh (HOMELAB_BACKUP_TEST_CONTAINER=1): scope = "system" (hosts/bananapi-m4.toml),
# which writes /etc, /usr/local and /var. systemctl is a stub that records the enabled timers; the pinned restic and
# rclone are copied from tests/lib.sh's $bin, so nothing is downloaded.
#   machines/backup/tests/module.sh        exit 0 = every check passed; the temp dir is kept on failure
set -Eeuo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if ((EUID == 0)) && [[ ${HOMELAB_BACKUP_TEST_CONTAINER:-} != 1 ]]; then
	echo "module.sh: as root this writes /etc, /usr/local and /var: run it through tests/in-docker.sh" >&2
	exit 2
fi
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$here/lib.sh"
install_sh=$backup_dir/debian/install.sh

if ((EUID == 0)); then
	host=bananapi-m4 bindir=/usr/local/bin confdir=/etc/homelab-backup unitdir=/etc/systemd/system
else
	export HOME=$tmp/home
	host=ubuntu-pc bindir=$HOME/.local/bin confdir=$HOME/.config/homelab-backup unitdir=$HOME/.config/systemd/user
fi
install -D -m 755 "$bin/restic" "$bindir/restic"
install -D -m 755 "$bin/rclone" "$bindir/rclone"
mkdir -p "$tmp/stub"
cat >"$tmp/stub/systemctl" <<'EOF'
#!/bin/sh
# systemctl stub: `enable --now UNIT` records UNIT in $SYSTEMCTL_STATE, `disable --now UNIT` drops it; is-enabled and
# is-active answer from there. The unit is the last argument.
for unit; do :; done
[ "$1" = --user ] && shift
case $1 in
daemon-reload) ;;
is-enabled | is-active) grep -qx "$unit" "$SYSTEMCTL_STATE" 2>/dev/null ;;
enable) echo "$unit" >>"$SYSTEMCTL_STATE" ;;
disable) grep -vx "$unit" "$SYSTEMCTL_STATE" >"$SYSTEMCTL_STATE.new" || true; mv "$SYSTEMCTL_STATE.new" "$SYSTEMCTL_STATE" ;;
*) echo "systemctl stub: unexpected $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$tmp/stub/systemctl"
export PATH=$tmp/stub:$PATH SYSTEMCTL_STATE=$tmp/enabled
# changed: a log shows a change debian/run made or would make (common.sh's `~ ...` drift lines, `wrote ...`)
changed() { grep -qE '^  (~|\+|wrote) ' "$1"; }

if ((EUID != 0)); then
	code=0
	"$install_sh" bananapi-m4 check >"$tmp/no-root.log" 2>&1 || code=$?
	((code == 6)) || fail "scope = \"system\" host without root: exit $code, expected 6 (see $tmp/no-root.log)"
	pass "bananapi-m4 (scope = \"system\") without root: refused, exit 6"
fi

# a host file without paths is refused before anything else (exit 2): debian/run from a copy of the module with a
# fixture host file, so hosts/ keeps only real hosts
mkdir -p "$tmp/fixture/debian" "$tmp/fixture/hosts"
cp "$backup_dir/debian/run" "$tmp/fixture/debian/run"
cp "$backup_dir/policy.toml" "$tmp/fixture/policy.toml"
echo 'scope = "user"' >"$tmp/fixture/hosts/no-paths.toml"
code=0
DT_DIR=$(cd "$backup_dir/../bananapi-m4" && pwd) DT_OPTIONS='{"host": "no-paths"}' "$tmp/fixture/debian/run" check \
	>"$tmp/no-paths.log" 2>&1 || code=$?
((code == 2)) || fail "host file without paths: exit $code, expected 2 (see $tmp/no-paths.log)"
grep -q 'paths must be a non-empty list' "$tmp/no-paths.log" || fail "host file without paths: reason not named"
pass "host file without paths: refused, exit 2"

code=0
"$install_sh" "$host" >"$tmp/plan1.log" 2>&1 || code=$?
((code == 3)) || fail "plan on a fresh system: exit $code, expected 3 (see $tmp/plan1.log)"
grep -q "+ $confdir/config.json" "$tmp/plan1.log" || fail "plan does not show config.json (see $tmp/plan1.log)"
"$install_sh" "$host" --commit >"$tmp/commit1.log" 2>&1 || fail "--commit (see $tmp/commit1.log)"
pass "$host: plan shows the drift (exit 3), --commit converges"

"$install_sh" "$host" check >"$tmp/check.log" 2>&1 || fail "check after --commit: exit $? (see $tmp/check.log)"
"$install_sh" "$host" >"$tmp/plan2.log" 2>&1 || fail "plan after --commit: exit $? (see $tmp/plan2.log)"
"$install_sh" "$host" --commit >"$tmp/commit2.log" 2>&1 || fail "second --commit (see $tmp/commit2.log)"
! changed "$tmp/commit2.log" || fail "second --commit changed something: $(cat "$tmp/commit2.log")"
pass "$host: check and plan clean after --commit, a second --commit writes nothing"

jq -e --arg host "$host" '.repository == "rclone:gdrive:homelab-backup-\($host)/restic"
	and .drive_folder == "gdrive:homelab-backup-\($host)" and (.restic_program | endswith("/restic"))' \
	"$confdir/config.json" >/dev/null || fail "config.json: $(cat "$confdir/config.json")"
[[ $(stat -c %a "$confdir") == 700 ]] || fail "$confdir is not 0700"
for timer in homelab-backup homelab-backup-check homelab-backup-drill; do
	grep -qx "$timer.timer" "$SYSTEMCTL_STATE" || fail "$timer.timer not enabled"
	[[ -f $unitdir/$timer.service && -f $unitdir/$timer.timer ]] || fail "$timer units not written"
done
[[ -L $bindir/homelab-backup && -x $bindir/homelab-backup ]] || fail "homelab-backup not linked into $bindir"
pass "$host: config.json points at gdrive:homelab-backup-$host/restic with the pinned restic, timers enabled, homelab-backup on PATH"

# watch units left from a host file that listed hosts once (neither host lists any now): disabled and removed
for suffix in service timer; do
	cp "$unitdir/homelab-backup.$suffix" "$unitdir/homelab-backup-watch.$suffix"
done
echo homelab-backup-watch.timer >>"$SYSTEMCTL_STATE"
code=0
"$install_sh" "$host" check >"$tmp/check-stale.log" 2>&1 || code=$?
((code == 3)) || fail "check with stale watch units: exit $code, expected 3"
"$install_sh" "$host" --commit >"$tmp/commit3.log" 2>&1 || fail "--commit with stale watch units (see $tmp/commit3.log)"
[[ ! -e $unitdir/homelab-backup-watch.service && ! -e $unitdir/homelab-backup-watch.timer ]] || fail "stale watch units not removed"
! grep -qx homelab-backup-watch.timer "$SYSTEMCTL_STATE" || fail "stale watch timer not disabled"
"$install_sh" "$host" check >"$tmp/check3.log" 2>&1 || fail "check after removing the stale units (see $tmp/check3.log)"
pass "$host: units of a verb the host file no longer asks for are disabled and removed"

# a regular file where the link goes is not replaced: the module refuses before it writes anything, and leaves it alone
libdir=$(dirname "$(readlink "$bindir/homelab-backup")")
rm "$bindir/homelab-backup" "$libdir/tasks_offsite.py"
echo foreign >"$bindir/homelab-backup"
code=0
"$install_sh" "$host" --commit >"$tmp/commit-foreign.log" 2>&1 || code=$?
((code == 1)) || fail "--commit over a foreign $bindir/homelab-backup: exit $code, expected 1 (see $tmp/commit-foreign.log)"
grep -q 'exists and is not a symlink' "$tmp/commit-foreign.log" || fail "foreign file: reason not named"
[[ ! -L $bindir/homelab-backup && $(cat "$bindir/homelab-backup") == foreign ]] || fail "foreign file was replaced"
[[ ! -e $libdir/tasks_offsite.py ]] || fail "--commit over a foreign $bindir/homelab-backup wrote $libdir before refusing"
pass "$host: a foreign file at $bindir/homelab-backup is refused before anything is written, and kept"

rm -rf "$tmp"
echo "all passed"
