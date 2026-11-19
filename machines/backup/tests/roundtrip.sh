#!/usr/bin/env bash
# End-to-end test of lib/homelab-backup against a local restic repository and a local rclone "Drive" (no Google):
# `run` refusing a missing repository and `init` creating it, SQLite dumps of live WAL databases (work dir on the
# state disk, cleaned up after a failure, refused when the disk is too full, one run or dump at a time), backup, full
# restore + diff, the restore drill (nightly snapshots only), idempotency of a second run, retention, a failing dump
# reported (status JSON + notify) while the snapshot is still written, the status copies in the Drive folder, the
# watch of other hosts, and the Tasks off-site copy. With docker and the postgres:16-alpine / redis:7-alpine
# images: the Postgres and Redis dumps, restored into fresh containers. As root (tests/in-docker.sh): the databases
# belong to another user, as on the system hosts, and must stay usable for that user.
# Needs sqlite3, jq, python3 and the pinned restic + rclone (tests/lib.sh); the Tasks part also needs the puller in
# ~/.local/lib/tasks-backup.
#   machines/backup/tests/roundtrip.sh        exit 0 = every check passed; the temp dir is kept on failure
set -Eeuo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$here/lib.sh"
script=$backup_dir/lib/homelab-backup
tasks_lib=${TASKS_BACKUP_LIB:-$HOME/.local/lib/tasks-backup}

containers=() writer=
cleanup() {
	[[ -z $writer ]] || kill "$writer" 2>/dev/null || true
	((${#containers[@]} == 0)) || docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# hb CONFIG VERB: homelab-backup VERB with CONFIG, its output appended to $tmp/<config>-<verb>.log
hb() { HOMELAB_BACKUP_CONFIG=$1 "$script" "$2" >>"$tmp/$(basename "$1" .json)-$2.log" 2>&1; }
no_work_dir() { ! compgen -G "$1/.work-*" >/dev/null || fail "$2: a dump work dir was left behind"; }
# the local stand-in for gdrive:, and a host's folder in it
drive=$tmp/gdrive
folder() { echo "$drive/homelab-backup-$1"; }

# as root the databases belong to uid 1000, as Home Assistant's belong to ultra on odroidn2. A command prefix, not
# a function: the background writer must be a direct child, so that killing $writer stops it.
owner=1000 as_owner=()
((EUID != 0)) || as_owner=(setpriv "--reuid=$owner" "--regid=$owner" --clear-groups)
data=$tmp/data
mkdir -p "$data/app/sub" "$tmp/state"
head -c 200000 /dev/urandom >"$data/app/blob.bin"
echo "drill target" >"$data/app/notes.txt"
echo "nested" >"$data/app/sub/file.txt"
mkdir -p "$data/app/.cache" && echo "cache" >"$data/app/.cache/skip-me"
if ((EUID == 0)); then
	chmod 755 "$tmp"
	chown -R "$owner:$owner" "$data"
fi
for db in one two; do
	"${as_owner[@]}" sqlite3 "$data/app/$db.db" "PRAGMA journal_mode=WAL; CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);
		WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5000) INSERT INTO t(v) SELECT hex(randomblob(16)) FROM n;" >/dev/null
done
# a writer that keeps its connection open (like Home Assistant) leaves a committed row only in the -wal file:
# the dump must still contain it
"${as_owner[@]}" python3 - "$data/app/one.db" "$data/writer.ready" <<'EOF' &
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("INSERT INTO t(v) VALUES ('in-the-wal')")
db.commit()
open(sys.argv[2], "w").close()
time.sleep(600)
EOF
writer=$!
for _ in $(seq 100); do [[ -e $data/writer.ready ]] && break; sleep 0.1; done
rm -f "$data/writer.ready"
[[ -s $data/app/one.db-wal ]] || fail "test setup: no WAL content"
touch -d '1 hour ago' "$data/app/notes.txt"
head -c 32 /dev/urandom | base64 >"$tmp/password"

# config.json with the real policy.toml, as debian/run renders it, but a local repository and a local rclone remote
python3 -c 'import json, sys, tomllib; print(json.dumps(tomllib.load(open(sys.argv[1], "rb"))))' "$backup_dir/policy.toml" |
	jq --arg tmp "$tmp" --arg data "$data" --arg bin "$bin" --arg tasks "$tasks_lib" --arg folder ":local:$(folder test-host)" '. as $p | {
		host: "test-host",
		drive_folder: $folder,
		repository: "\($tmp)/repo",
		password_file: "\($tmp)/password",
		rclone_config: "\($tmp)/rclone.conf",
		rclone_program: "\($bin)/rclone",
		restic_program: "\($bin)/restic",
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
		watch: [],
		policy: $p
	}' >"$tmp/config.json"
touch "$tmp/rclone.conf"
config=$tmp/config.json
# the host's Drive folder (in production restic init creates it; here the repository is local)
mkdir -p "$(folder test-host)"
export HOMELAB_BACKUP_CONFIG=$config
export RESTIC_REPOSITORY=$tmp/repo RESTIC_PASSWORD_FILE=$tmp/password RESTIC_CACHE_DIR=$tmp/cache
staging=$tmp/state/staging

# 0. no repository yet: run refuses to create one, only init does (and a second init leaves it alone)
if hb "$config" run; then fail "run without a repository succeeded"; fi
jq -e '.ok == false and (.message | test("do not init.*homelab-backup init"))' "$tmp/state/status-run.json" >/dev/null ||
	fail "missing repository not reported as such: $(cat "$tmp/state/status-run.json")"
[[ ! -e $tmp/repo ]] || fail "run created a repository"
hb "$config" init || fail "init"
id=$(restic cat config | jq -r .id)
hb "$config" init || fail "second init"
[[ $(restic cat config | jq -r .id) == "$id" ]] || fail "second init replaced the repository"
pass "no repository: run fails and names init, init creates it, a second init keeps it"

# 1. first run: dumps + backup + retention, and the status copy in the Drive folder
hb "$config" run || fail "first run"
jq -e .ok "$tmp/state/status-run.json" >/dev/null || fail "status-run.json is not ok"
[[ $(restic snapshots --json | jq length) == 1 ]] || fail "expected 1 snapshot"
no_work_dir "$tmp/state" "first run"
cmp -s "$tmp/state/status-run.json" "$(folder test-host)/status/status-run.json" || fail "status-run.json not copied to the Drive folder"
pass "first run: 1 snapshot, status ok and copied to the Drive folder, no work dir left"
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

# 4. the drill, also after a snapshot made by hand (no dump dir in it): it restores the latest nightly one
restic backup --host test-host "$data/app/sub" -q >/dev/null
hb "$config" drill || fail "drill"
jq -e .ok "$tmp/state/status-drill.json" >/dev/null || fail "status-drill.json is not ok"
pass "drill: latest nightly snapshot restored past a manual one, dumps match SHA256SUMS, integrity_check ok, drill_file equal"

# 5. second run with nothing changed: no new file content
hb "$config" run || fail "second run"
summary=$(restic snapshots --tag nightly --json | jq 'max_by(.time) | .summary')
[[ $(jq '.data_blobs + .files_new + .files_changed' <<<"$summary") == 0 ]] ||
	fail "second run changed content: $summary"
pass "second run: $(jq -c '{files_new, files_changed, files_unmodified, data_blobs}' <<<"$summary")"

# retention: 20 backdated nightly snapshots, then a run applies keep-daily 7 / weekly 4 / monthly 12 to the nightly
# ones only; the manual snapshot stays
for days in $(seq 1 20); do
	restic backup --host test-host --tag nightly --time "$(date -d "$days days ago" '+%Y-%m-%d 12:00:00')" "$data" -q >/dev/null
done
before=$(restic snapshots --tag nightly --json | jq length)
hb "$config" run || fail "retention run"
after=$(restic snapshots --tag nightly --json | jq length)
days_kept=$(restic snapshots --tag nightly --json | jq '[.[].time[:10]] | unique | length')
((after < before && days_kept >= 7)) || fail "retention: $before snapshots before, $after after, $days_kept days"
[[ $(restic snapshots --json | jq '[.[] | select(.tags == null)] | length') == 1 ]] || fail "retention removed the manual snapshot"
pass "retention: $((before + 1)) nightly snapshots -> $after ($days_kept distinct days kept), manual snapshot untouched"
hb "$config" check || fail "check"
pass "check --read-data-subset ok"

# 6. a failing dump: reported (status + notify), the rest is still backed up
jq '.dumps += [{type: "sqlite", name: "gone", source: "/nonexistent/x.db"}]' "$config" >"$tmp/config-bad.json"
if hb "$tmp/config-bad.json" run; then fail "run with a missing db succeeded"; fi
jq -e '.ok == false and (.message | test("gone"))' "$tmp/state/status-run.json" >/dev/null || fail "failure not in status-run.json"
grep -q "run failed: .*gone" "$tmp/notified" || fail "notify command not called"
# the listing first: `restic ls | grep -q` would fail under pipefail when grep exits before restic finished writing
listing=$(restic ls latest)
grep -qx "$staging/one.sqlite" <<<"$listing" || fail "the other dumps were not backed up"
pass "failing dump: run exits non-zero, status + notify carry the reason, snapshot still written"

# 6b. the dump verb alone: a source that is not a database fails the verb, keeps the previous good dump and leaves
# no work dir behind; so does a state disk too full for the dump (df reports 1000 bytes free)
good=$(sha256sum <"$staging/one.sqlite")
head -c 4096 /dev/urandom >"$tmp/garbage.db"
jq --arg g "$tmp/garbage.db" '.dumps[0].source = $g' "$config" >"$tmp/config-garbage.json"
if hb "$tmp/config-garbage.json" dump; then fail "dump of a garbage db succeeded"; fi
jq -e '.ok == false and (.message | test("one"))' "$tmp/state/status-dump.json" >/dev/null || fail "failed dump not in status-dump.json"
[[ $(sha256sum <"$staging/one.sqlite") == "$good" ]] || fail "failed dump replaced the previous good dump"
no_work_dir "$tmp/state" "garbage dump"
mkdir -p "$tmp/fake-df"
printf '#!/bin/sh\necho Avail\necho 1000\n' >"$tmp/fake-df/df"
chmod +x "$tmp/fake-df/df"
if PATH=$tmp/fake-df:$PATH hb "$config" dump; then fail "dump onto a full disk succeeded"; fi
grep -q "dump one: needs [0-9]* bytes in $tmp/state, only 1000 free" "$tmp/config-dump.log" || fail "full disk not reported (see $tmp/config-dump.log)"
[[ $(sha256sum <"$staging/one.sqlite") == "$good" ]] || fail "dump onto a full disk replaced the previous good dump"
no_work_dir "$tmp/state" "dump onto a full disk"
pass "dump verb: a garbage source and a too-full disk fail the verb, keep the previous dump, leave no work dir"

# 6c. one run or dump at a time: a dump started while another holds the lock waits for it, then succeeds
flock "$tmp/state/.lock-dump" sh -c 'touch "$1"; sleep 2' sh "$tmp/locked" &
for _ in $(seq 100); do [[ -e $tmp/locked ]] && break; sleep 0.1; done
hb "$config" dump || fail "dump after waiting for the lock"
grep -q "another run or dump is in progress: waiting for it" "$tmp/config-dump.log" || fail "dump did not wait for the lock"
wait $!
pass "a dump waits for a run or dump in progress"

# 7. missing secret: clear message, no restic call
mv "$tmp/password" "$tmp/password.away"
if hb "$config" run; then fail "run without the password file succeeded"; fi
jq -e '.message | test("password file")' "$tmp/state/status-run.json" >/dev/null || fail "missing secret not reported"
mv "$tmp/password.away" "$tmp/password"
pass "missing password file reported"

# 8. Tasks off-site copy: new runs copied, remote-only runs past the puller's retention purged, local ones kept
tasks_remote=$(folder test-host)/tasks
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
	mkdir -p "$tasks_remote/20200102T000000Z" && echo old >"$tasks_remote/20200102T000000Z/x"
	hb "$config" tasks || fail "tasks"
	[[ -f $tasks_remote/$now/postgres.dump.age ]] || fail "new run not copied"
	[[ -d $tasks_remote/$old_local ]] || fail "a run the puller still holds was removed remotely"
	[[ ! -e $tasks_remote/20200102T000000Z ]] || fail "expired remote-only run not purged"
	[[ ! -e $tasks_remote/.partial-$now && ! -e $tasks_remote/state.json ]] || fail "non-run files copied"
	hb "$config" tasks || fail "second tasks run"
	pass "tasks off-site: copied, retention applied, partial/state skipped, rerun ok"
else
	echo "skip - tasks off-site: no puller in $tasks_lib"
fi

# 9. a host without [[dumps]] (bananapi-m4, macmini): run and drill pass on an empty dump staging dir
jq --arg tmp "$tmp" '.dumps = [] | .state_dir = "\($tmp)/state-nodumps" | .repository = "\($tmp)/repo-nodumps"' \
	"$config" >"$tmp/config-nodumps.json"
hb "$tmp/config-nodumps.json" init || fail "init without dumps"
hb "$tmp/config-nodumps.json" run || fail "run without dumps"
hb "$tmp/config-nodumps.json" drill || fail "drill without dumps"
pass "host without dumps: run + drill ok"

# 10. watch: each watched host's folder in the (here local) Drive: its snapshot files and the status copies
watched() { # HOST SNAPSHOT_AGE RUN_AGE [VERB OK]: a host folder with one snapshot file and status copies
	local dir
	dir=$(folder "$1")
	mkdir -p "$dir/restic/snapshots" "$dir/status"
	echo x >"$dir/restic/snapshots/a" && touch -d "$2" "$dir/restic/snapshots/a"
	jq -n --arg host "$1" --arg at "$(date -u -d "$3" +%Y-%m-%dT%H:%M:%SZ)" \
		'{host: $host, verb: "run", ok: true, message: "", finished_at: $at}' >"$dir/status/status-run.json"
	if [[ -n ${4:-} ]]; then
		jq -n --arg host "$1" --arg verb "$4" --argjson ok "$5" \
			'{host: $host, verb: $verb, ok: $ok, message: "it broke", finished_at: "2026-01-01T00:00:00Z"}' >"$dir/status/status-$4.json"
	fi
}
watch_config() {
	jq --arg drive "$drive" '.watch = [$ARGS.positional[] | {host: ., folder: ":local:\($drive)/homelab-backup-\(.)"}]' \
		"$config" --args "$@" >"$tmp/config-watch.json"
}
watched fresh '2 hours ago' '2 hours ago' check true
watched stale '3 days ago' '3 days ago'
watched failing '1 hour ago' '1 hour ago' drill false
watch_config fresh
hb "$tmp/config-watch.json" watch || fail "watch of a healthy host"
grep -q "watch: fresh: newest snapshot is 2h old" "$tmp/config-watch-watch.log" || fail "watch did not look at the newest snapshot"
watch_config fresh stale failing never
if hb "$tmp/config-watch.json" watch; then fail "watch of a stale, a failing and a missing host succeeded"; fi
jq -e '.ok == false and (.message | test("stale: newest snapshot is 7[0-9]h old")) and (.message | test("stale: last run finished at"))
	and (.message | test("failing: drill failed: it broke")) and (.message | test("never: 0 folders named homelab-backup-never"))
	and (.message | test("fresh") | not)' "$tmp/state/status-watch.json" >/dev/null ||
	fail "watch status: $(cat "$tmp/state/status-watch.json")"
grep -q "watch failed: .*stale" "$tmp/notified" || fail "watch failure not notified"
pass "watch: a healthy host passes; a stale, a failing and a missing one fail the verb and notify"

# 11. Postgres and Redis dumps from real containers, restored into fresh ones
if docker info >/dev/null 2>&1 && docker image inspect postgres:16-alpine redis:7-alpine >/dev/null 2>&1; then
	pg=homelab-backup-test-pg-$$ redis=homelab-backup-test-redis-$$ redis2=homelab-backup-test-redis2-$$
	containers=("$pg" "$redis" "$redis2")
	docker run -d --name "$pg" -e POSTGRES_USER=app -e POSTGRES_PASSWORD=test -e POSTGRES_DB=app postgres:16-alpine >/dev/null
	docker run -d --name "$redis" redis:7-alpine redis-server --appendonly yes >/dev/null
	# the image's init runs a temporary server first: wait for the final one
	for _ in $(seq 120); do
		docker logs "$pg" 2>&1 | grep -q "PostgreSQL init process complete" && docker exec "$pg" pg_isready -q -U app -d app && break
		sleep 0.5
	done
	docker exec "$pg" psql -q -U app -d app -c "CREATE TABLE t(v text); INSERT INTO t SELECT md5(i::text) FROM generate_series(1, 1000) i;" ||
		fail "test setup: postgres"
	docker exec "$redis" redis-cli SET probe from-the-test >/dev/null || fail "test setup: redis"
	jq --arg tmp "$tmp" --arg pg "$pg" --arg redis "$redis" '.dumps = [
			{type: "postgres", name: "pg", container: $pg, database: "app", user: "app"},
			{type: "redis", name: "redis", container: $redis}
		] | .state_dir = "\($tmp)/state-docker" | .repository = "\($tmp)/repo-docker"' \
		"$config" >"$tmp/config-docker.json"
	hb "$tmp/config-docker.json" init || fail "init (docker)"
	hb "$tmp/config-docker.json" run || fail "run with postgres + redis"
	hb "$tmp/config-docker.json" drill || fail "drill with postgres + redis"
	restic -r "$tmp/repo-docker" restore latest --target "$tmp/restore-docker" >/dev/null
	dumps=$tmp/restore-docker$tmp/state-docker/staging
	docker exec "$pg" createdb -U app restored
	docker exec -i "$pg" pg_restore -U app -d restored <"$dumps/pg.pgdump" || fail "pg_restore of the dump"
	[[ $(docker exec "$pg" psql -tA -U app -d restored -c "SELECT count(*) FROM t") == 1000 ]] || fail "restored postgres table"
	docker create --name "$redis2" redis:7-alpine redis-server --appendonly yes >/dev/null
	docker cp "$dumps/redis/." "$redis2:/data/"
	docker start "$redis2" >/dev/null
	for _ in $(seq 60); do docker exec "$redis2" redis-cli PING 2>/dev/null | grep -q PONG && break; sleep 0.5; done
	[[ $(docker exec "$redis2" redis-cli GET probe) == from-the-test ]] || fail "restored redis has no probe key"
	# failing dumps of both kinds leave nothing behind, in staging or in the work dirs
	jq '.dumps[0].container = "homelab-backup-test-none" | .dumps[1].container = "homelab-backup-test-none"' \
		"$tmp/config-docker.json" >"$tmp/config-docker-bad.json"
	if hb "$tmp/config-docker-bad.json" dump; then fail "dumps from a missing container succeeded"; fi
	[[ $(cd "$tmp/state-docker/staging" && find . -mindepth 1 -maxdepth 1 | sort | tr '\n' ' ') == "./SHA256SUMS ./pg.pgdump ./redis " ]] ||
		fail "failed dumps left files in staging: $(ls -A "$tmp/state-docker/staging")"
	no_work_dir "$tmp/state-docker" "failed postgres + redis dumps"
	pass "postgres + redis: dumped from live containers, drill ok, restored into fresh ones; failed dumps leave nothing"
else
	echo "skip - postgres + redis: no docker or no postgres:16-alpine / redis:7-alpine image"
fi

# 12. as root: the dumps opened the databases as root, yet left nothing root-owned next to them, and the owner can
# still write (SQLite gives the -wal/-shm it creates as root to the database's owner)
if ((EUID == 0)); then
	hb "$config" dump || fail "dump as root"
	[[ -z $(find "$data/app" -user 0) ]] || fail "root-owned files next to the databases: $(find "$data/app" -user 0)"
	# stat runs from inside sqlite3: a root connection's -wal/-shm exist only while it is open (the query's own
	# output is buffered and may come out after stat's)
	owners=$(sqlite3 "$data/app/two.db" "SELECT count(*) FROM t" ".shell stat -c '%n %u' $data/app/two.db-wal $data/app/two.db-shm" |
		grep "^$data/app/two.db-")
	[[ $owners == "$data/app/two.db-wal $owner"$'\n'"$data/app/two.db-shm $owner" ]] ||
		fail "-wal/-shm created by a root connection are not the owner's: $owners"
	"${as_owner[@]}" sqlite3 "$data/app/two.db" "INSERT INTO t(v) VALUES ('owner-after-root')" || fail "owner cannot write after the root dump"
	pass "as root: databases of uid $owner dumped, nothing root-owned left, owner still writes"
fi

rm -rf "$tmp"
echo "all passed"
