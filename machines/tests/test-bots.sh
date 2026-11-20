#!/usr/bin/env bash
# The bot-hosting modules, each run directly (DT_OPTIONS as the engine sets it), without root: service-users refuses a
# user without uid/home and a comment passwd cannot hold; bots refuses a stack without its compose file and reports a
# missing link, directory and Bun as drift; actions-runners reports an unpacked-or-unregistered runner as drift and
# keeps going to the next one; the board's machine.toml declares what the units and the backup host file expect.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

modules=$MACHINES/bananapi-m4/modules
module() {
	run env DT_DIR="$MACHINES/bananapi-m4" DT_OPTIONS="$3" DT_CONFIG="${4:-{\}}" "$modules/$1/run" "$2"
}

module service-users check '{"users": [{"name": "dt-nouid", "home": "/var/lib/dt"}]}'
expect "service-users: a user without uid exits 2" test "$code" = 2
module service-users check '{"users": [{"name": "dt-colon", "uid": 940, "home": "/var/lib/dt", "comment": "a: b"}]}'
expect "service-users: a comment with a colon exits 2" test "$code" = 2
expect "  ... naming the rule" contains "$err" "colon"
module service-users plan '{"users": [{"name": "dt-new-user", "uid": 940, "home": "/nonexistent"}]}'
expect "service-users plan: a missing user drifts (exit 3)" test "$code" = 3
expect "  ... naming uid and home" contains "$out" "create system user dt-new-user (uid 940, home /nonexistent)"
module service-users check '{"users": [{"name": "root", "uid": 5, "home": "/root"}]}'
expect "service-users: an existing user with another uid is refused (exit 1)" test "$code" = 1
expect "  ... as a migration" contains "$err" "renumbering is a migration"
module service-users check '{"users": [{"name": "dt-uid-taken", "uid": 0, "home": "/x"}]}'
expect "service-users: a new user on a taken uid is refused (exit 1)" test "$code" = 1
expect "  ... naming the owner" contains "$err" "uid 0 already belongs to root"

module bots check '{"stacks": ["dt-no-such-stack"]}'
expect "bots: a stack without compose/<name>/compose.yaml exits 2" test "$code" = 2
module bots plan "{\"links\": {\"$WORK/link\": \"/nonexistent/target\"}, \"dirs\": [{\"path\": \"$WORK/dir\"}],
	\"bun\": [{\"prefix\": \"$WORK/bun\", \"version\": \"1.4.2\", \"sha256\": \"0\"}]}"
expect "bots plan: missing link, directory and Bun drift (exit 3)" test "$code" = 3
expect "  ... the link" contains "$out" "link $WORK/link -> /nonexistent/target"
expect "  ... the directory" contains "$out" "create $WORK/dir"
expect "  ... the Bun runtime" contains "$out" "$WORK/bun/bin/bun: Bun none -> 1.4.2"
expect "  ... and changes nothing" test ! -e "$WORK/link" -a ! -e "$WORK/dir"
mkdir -p "$WORK/isdir"
module bots check "{\"links\": {\"$WORK/isdir\": \"/nonexistent/target\"}}"
expect "bots: a directory where a link is declared exits 1" test "$code" = 1

runners="{\"version\": \"2.338.2\", \"sha256\": \"0\", \"runners\": [
	{\"repo\": \"o/a\", \"name\": \"dt-a\", \"user\": \"root\", \"dir\": \"$WORK/a\", \"labels\": [\"x\"]},
	{\"repo\": \"o/b\", \"name\": \"dt-b\", \"user\": \"root\", \"dir\": \"$WORK/b\", \"labels\": [\"y\"]}]}"
module actions-runners plan "$runners"
expect "actions-runners plan: unpacked and unregistered runners drift (exit 3)" test "$code" = 3
expect "  ... the first runner" contains "$out" "dt-a: not registered"
expect "  ... and the next one too" contains "$out" "dt-b: not registered"

# The declaration itself: every unit file listed exists, and the backup host file covers each bot's database.
toml=$MACHINES/bananapi-m4/machine.toml
missing=$(python3 - "$toml" "$MACHINES/bananapi-m4/units/system" <<'PY'
import pathlib, sys, tomllib
config = tomllib.load(open(sys.argv[1], "rb"))
units = pathlib.Path(sys.argv[2])
print(" ".join(u for u in config["modules"]["systemd-units"]["system"] if not (units / u).is_file()))
PY
)
expect "machine.toml: every systemd-units file exists (missing: ${missing:-none})" test -z "$missing"
hostfile=$(<"$MACHINES/backup/hosts/bananapi-m4.toml")
for db in /srv/hyper-summary-bot/data/hyper-summary.db /var/www/ExpenseSyncBot/data/expenses.db \
	/opt/hypercal/data/calendar.db; do
	expect "backup host file dumps $db" contains "$hostfile" "source = \"$db\""
done

done_testing
