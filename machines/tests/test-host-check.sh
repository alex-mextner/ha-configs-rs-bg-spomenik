#!/usr/bin/env bash
# install.sh's host guard: a machine.toml whose [modules.system] hostname is not this box (or is missing) converges
# nothing (exit 2) except `list`; --force-host overrides a mismatch only with this box's own name. The machine files
# used here switch every module off, so a run that passes the guard changes nothing.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

INSTALL=$MACHINES/bananapi-m4/install.sh
host=$(hostname)
# past the guard: 0 as root (nothing selected), 6 (needs root) otherwise
passed=$( ((EUID == 0)) && echo 0 || echo 6)

# machine FILE [HOSTNAME]: a machine.toml with every module off, declaring HOSTNAME
machine() {
	local file=$WORK/$1 name
	for name in "$MACHINES"/bananapi-m4/modules/*/; do
		name=$(basename "$name")
		[[ $name == system ]] || printf '[modules.%s]\nenabled = false\n' "$name"
	done >"$file"
	printf '[modules.system]\nenabled = false\n' >>"$file"
	[[ -z ${2:-} ]] || printf 'hostname = "%s"\n' "$2" >>"$file"
	echo "$file"
}
this=$(machine this.toml "$host")
other=$(machine other.toml not-this-box)
nameless=$(machine nameless.toml)

run "$INSTALL" --machine "$this" check
expect "this box's file passes the guard (exit $passed)" test "$code" = "$passed"
run "$INSTALL" --machine "$this" --commit
expect "  ... for --commit too" test "$code" = "$passed"

for cmd in "" check --commit; do
	# shellcheck disable=SC2086 # "" is the preview: no argument at all
	run "$INSTALL" --machine "$other" $cmd
	expect "another box's file: ${cmd:-preview} exits 2" test "$code" = 2
	expect "  ... naming both hosts" contains "$err" "is for not-this-box, this is $host"
done
run "$INSTALL" --machine "$other" list
expect "another box's file: list still works (exit 0)" test "$code" = 0
run "$INSTALL" --machine "$other" --force-host "$host" check
expect "--force-host with this box's name overrides (exit $passed)" test "$code" = "$passed"
run "$INSTALL" --machine "$other" --force-host not-this-box check
expect "--force-host with any other name still stops (exit 2)" test "$code" = 2

run "$INSTALL" --machine "$nameless" check
expect "a file without a hostname exits 2" test "$code" = 2
expect "  ... saying so" contains "$err" "declares no [modules.system] hostname"
run "$INSTALL" --machine "$nameless" --force-host "$host" --commit
expect "  ... --force-host does not replace it" test "$code" = 2
run "$INSTALL" --machine "$nameless" list
expect "  ... list still works (exit 0)" test "$code" = 0

# the shipped machine files name their boxes, so on any other box (this one) they stop
for file in "$MACHINES/bananapi-m4/machine.toml" "$MACHINES"/dotfiles/debian/hosts/*.toml; do
	run "$INSTALL" --machine "$file" check
	expect "${file#"$MACHINES"/} stops on $host (exit 2)" test "$code" = 2
	expect "  ... because it is for another host" contains "$err" "is for "
done

done_testing
