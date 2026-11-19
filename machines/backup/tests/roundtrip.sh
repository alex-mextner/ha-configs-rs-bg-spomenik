#!/usr/bin/env bash
# End-to-end test of lib/homelab-backup against a local restic repository (no Google Drive, no root):
# SQLite dumps of live WAL databases, backup, snapshot list, full restore + diff, the restore drill, idempotency
# of a second run, a failing dump reported (status JSON + notify) while the snapshot is still written, and the
# Tasks off-site copy with retention against a local rclone remote. Needs restic, sqlite3, jq, python3 and rclone
# (the module installs it in ~/.local/bin); the Tasks part also needs the puller in ~/.local/lib/tasks-backup.
#   machines/backup/tests/roundtrip.sh        exit 0 = every check passed; the temp dir is kept on failure
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
backup_dir=$(dirname "$here")
script=$backup_dir/lib/homelab-backup
rclone=$(command -v rclone || echo "$HOME/.local/bin/rclone")
tasks_lib=${TASKS_BACKUP_LIB:-$HOME/.local/lib/tasks-backup}

tmp=$(mktemp -d /var/tmp/homelab-backup-test.XXXXXX)
pass() { echo "ok - $*"; }
fail() {
	echo "FAIL - $*" >&2
	echo "kept for inspection: $tmp" >&2
	exit 1
}
data=$tmp/data
mkdir -p "$data/app/sub" "$tmp/state"
head -c 200000 /dev/urandom >"$data/app/blob.bin"
echo "drill target" >"$data/app/notes.txt"
echo "nested" >"$data/app/sub/file.txt"
mkdir -p "$data/app/.cache" && echo "cache" >"$data/app/.cache/skip-me"
for db in one two; do
	sqlite3 "$data/app/$db.db" "PRAGMA journal_mode=WAL; CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);
		WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5000) INSERT INTO t(v) SELECT hex(randomblob(16)) FROM n;" >/dev/null
done
# a writer that keeps its connection open (like Home Assistant) leaves a committed row only in the -wal file:
# the dump must still contain it
python3 - "$data/app/one.db" "$tmp/writer.ready" <<'EOF' &
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("INSERT INTO t(v) VALUES ('in-the-wal')")
db.commit()
open(sys.argv[2], "w").close()
time.sleep(600)
EOF
writer=$!
for _ in $(seq 100); do [[ -e $tmp/writer.ready ]] && break; sleep 0.1; done
[[ -s $data/app/one.db-wal ]] || fail "test setup: no WAL content"
touch -d '1 hour ago' "$data/app/notes.txt"
head -c 32 /dev/urandom | base64 >"$tmp/password"

# config.json with the real policy.toml, as debian/run renders it, but a local repository and a local rclone remote
python3 -c 'import json, sys, tomllib; print(json.dumps(tomllib.load(open(sys.argv[1], "rb"))))' "$backup_dir/policy.toml" |
	jq --arg tmp "$tmp" --arg data "$data" --arg rclone "$rclone" --arg tasks "$tasks_lib" '
	(. + {remote: ":local", root: "\($tmp)/gdrive"}) as $p | {
		host: "test-host",
		repository: "\($tmp)/repo",
		password_file: "\($tmp)/password",
		rclone_config: "\($tmp)/rclone.conf",
		rclone_program: $rclone,
		state_dir: "\($tmp)/state",
		cache_dir: "\($tmp)/cache",
		paths: [$data, "\($tmp)/does-not-exist"],
		exclude: [".cache"],
		dumps: [
			{type: "sqlite", name: "one", source: "\($data)/app/one.db"},
			{type: "sqlite", name: "two", source: "\($data)/app/two.db"}
		],
		drill_file: "\($data)/app/notes.txt",
		notify: ["sh", "-c", "printf %s\\\\n \"$1\" >> \($tmp)/notified", "notify"],
		tasks_offsite: {source: "\($tmp)/tasks-src", pythonpath: $tasks},
		policy: $p
	}' >"$tmp/config.json"
touch "$tmp/rclone.conf"
export HOMELAB_BACKUP_CONFIG=$tmp/config.json
export RESTIC_REPOSITORY=$tmp/repo RESTIC_PASSWORD_FILE=$tmp/password RESTIC_CACHE_DIR=$tmp/cache
staging=$tmp/state/staging

# 1. first run: init + dumps + backup + retention
"$script" run >"$tmp/run1.log" 2>&1 || fail "first run (see $tmp/run1.log)"
jq -e .ok "$tmp/state/status-run.json" >/dev/null || fail "status-run.json is not ok"
[[ $(restic snapshots --json | jq length) == 1 ]] || fail "expected 1 snapshot"
pass "first run: 1 snapshot, status ok"
echo "--- snapshots"
"$script" snapshots

# 2. the dump holds the WAL-only row; the live database files are not in the snapshot, the dumps are
[[ $(sqlite3 "$staging/one.sqlite" "SELECT count(*) FROM t WHERE v = 'in-the-wal'") == 1 ]] || fail "WAL row missing from the dump"
kill "$writer" && wait "$writer" 2>/dev/null || true
listing=$(restic ls latest)
grep -qx "$staging/one.sqlite" <<<"$listing" || fail "dump not in the snapshot"
! grep -qE '/app/(one|two)\.db(-wal|-shm)?$' <<<"$listing" || fail "live SQLite files are in the snapshot"
! grep -q '/.cache/' <<<"$listing" || fail "excluded .cache is in the snapshot"
pass "dumps in the snapshot (row only in the WAL of an open writer included), live db files and .cache excluded"

# 3. full restore: identical to the source except the excluded files
restic restore latest --target "$tmp/restore" >/dev/null
diff -r --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude=.cache "$data" "$tmp/restore$data" ||
	fail "restored tree differs"
diff -r "$staging" "$tmp/restore$staging" || fail "restored dumps differ"
for db in one two; do
	cmp -s <(sqlite3 "$data/app/$db.db" .dump) <(sqlite3 "$tmp/restore$staging/$db.sqlite" .dump) ||
		fail "restored dump $db differs from the live database"
done
pass "restore: diff of files and of the SQL content is empty"

# 4. the drill
"$script" drill >"$tmp/drill.log" 2>&1 || fail "drill (see $tmp/drill.log)"
jq -e .ok "$tmp/state/status-drill.json" >/dev/null || fail "status-drill.json is not ok"
pass "drill: restored dumps match SHA256SUMS, integrity_check ok, drill_file equal"

# 5. second run with nothing changed: no new file content, retention keeps one snapshot per day
"$script" run >"$tmp/run2.log" 2>&1 || fail "second run (see $tmp/run2.log)"
summary=$(restic snapshots --json | jq 'max_by(.time) | .summary')
[[ $(jq '.data_blobs + .files_new + .files_changed' <<<"$summary") == 0 ]] ||
	fail "second run changed content: $summary"
pass "second run: $(jq -c '{files_new, files_changed, files_unmodified, data_blobs}' <<<"$summary")"

# retention: 20 backdated daily snapshots, then a run applies keep-daily 7 / weekly 4 / monthly 12
for days in $(seq 1 20); do
	restic backup --host test-host --time "$(date -d "$days days ago" '+%Y-%m-%d 12:00:00')" "$data" -q >/dev/null
done
before=$(restic snapshots --json | jq length)
"$script" run >"$tmp/run-retention.log" 2>&1 || fail "retention run (see $tmp/run-retention.log)"
after=$(restic snapshots --json | jq length)
days_kept=$(restic snapshots --json | jq '[.[].time[:10]] | unique | length')
((after < before && days_kept >= 7)) || fail "retention: $before snapshots before, $after after, $days_kept days"
pass "retention: $((before + 1)) snapshots -> $after ($days_kept distinct days kept)"
"$script" check >"$tmp/check.log" 2>&1 || fail "check (see $tmp/check.log)"
pass "check --read-data-subset ok"

# 6. a failing dump: reported (status + notify), the rest is still backed up
jq '.dumps += [{type: "sqlite", name: "gone", source: "/nonexistent/x.db"}]' "$tmp/config.json" >"$tmp/config-bad.json"
if HOMELAB_BACKUP_CONFIG=$tmp/config-bad.json "$script" run >"$tmp/run3.log" 2>&1; then fail "run with a missing db succeeded"; fi
jq -e '.ok == false and (.message | test("gone"))' "$tmp/state/status-run.json" >/dev/null || fail "failure not in status-run.json"
grep -q "run failed: .*gone" "$tmp/notified" || fail "notify command not called"
restic ls latest | grep -qx "$staging/one.sqlite" || fail "the other dumps were not backed up"
pass "failing dump: run exits non-zero, status + notify carry the reason, snapshot still written"

# 6b. the dump verb alone (NixOS backupPrepareCommand): a source that is not a database fails the verb and keeps
# the previous good dump instead of replacing it
good=$(sha256sum <"$staging/one.sqlite")
head -c 4096 /dev/urandom >"$tmp/garbage.db"
jq --arg g "$tmp/garbage.db" '.dumps[0].source = $g' "$tmp/config.json" >"$tmp/config-garbage.json"
if HOMELAB_BACKUP_CONFIG=$tmp/config-garbage.json "$script" dump >"$tmp/dump-bad.log" 2>&1; then fail "dump of a garbage db succeeded"; fi
jq -e '.ok == false and (.message | test("one"))' "$tmp/state/status-dump.json" >/dev/null || fail "failed dump not in status-dump.json"
[[ $(sha256sum <"$staging/one.sqlite") == "$good" ]] || fail "failed dump replaced the previous good dump"
"$script" dump >"$tmp/dump-good.log" 2>&1 || fail "dump (see $tmp/dump-good.log)"
pass "dump verb: a failing source exits non-zero, is recorded, and leaves the previous dump untouched"

# 7. missing secret: clear message, no restic call
mv "$tmp/password" "$tmp/password.away"
if "$script" run >"$tmp/run4.log" 2>&1; then fail "run without the password file succeeded"; fi
jq -e '.message | test("password file")' "$tmp/state/status-run.json" >/dev/null || fail "missing secret not reported"
mv "$tmp/password.away" "$tmp/password"
pass "missing password file reported"

# 8. Tasks off-site copy: new runs copied, remote-only runs past the puller's retention purged, local ones kept
if [[ -d $tasks_lib/tasks_backup ]]; then
	src=$tmp/tasks-src
	base=$(date -u +%s)
	run_id() { date -u -d "@$1" +%Y%m%dT%H%M%SZ; }
	now=$(run_id "$base")
	old_local=$(run_id "$((base - 60000 * 3600))")
	# 14 recent runs (more than the puller's keep_recent = 12) and one old run the puller still holds locally
	for hours in $(seq 0 13) 60000; do
		run=$(run_id "$((base - hours * 3600))")
		mkdir -p "$src/$run" && echo "$run" >"$src/$run/postgres.dump.age"
	done
	mkdir -p "$src/.partial-$now" && echo partial >"$src/.partial-$now/x" && echo '{}' >"$src/state.json"
	mkdir -p "$tmp/gdrive/tasks/20200102T000000Z" && echo old >"$tmp/gdrive/tasks/20200102T000000Z/x"
	"$script" tasks >"$tmp/tasks.log" 2>&1 || fail "tasks (see $tmp/tasks.log)"
	[[ -f $tmp/gdrive/tasks/$now/postgres.dump.age ]] || fail "new run not copied"
	[[ -d $tmp/gdrive/tasks/$old_local ]] || fail "a run the puller still holds was removed remotely"
	[[ ! -e $tmp/gdrive/tasks/20200102T000000Z ]] || fail "expired remote-only run not purged"
	[[ ! -e $tmp/gdrive/tasks/.partial-$now && ! -e $tmp/gdrive/tasks/state.json ]] || fail "non-run files copied"
	"$script" tasks >>"$tmp/tasks.log" 2>&1 || fail "second tasks run"
	pass "tasks off-site: copied, retention applied, partial/state skipped, rerun ok"
else
	echo "skip - tasks off-site: no puller in $tasks_lib"
fi

# 9. a host without [[dumps]] (bananapi-m4, macmini): run and drill pass on an empty dump staging dir
jq --arg tmp "$tmp" '.dumps = [] | .state_dir = "\($tmp)/state-nodumps" | .repository = "\($tmp)/repo-nodumps"' \
	"$tmp/config.json" >"$tmp/config-nodumps.json"
HOMELAB_BACKUP_CONFIG=$tmp/config-nodumps.json "$script" run >"$tmp/nodumps.log" 2>&1 || fail "run without dumps (see $tmp/nodumps.log)"
HOMELAB_BACKUP_CONFIG=$tmp/config-nodumps.json "$script" drill >>"$tmp/nodumps.log" 2>&1 || fail "drill without dumps (see $tmp/nodumps.log)"
pass "host without dumps: run + drill ok"

rm -rf "$tmp"
echo "all passed"
