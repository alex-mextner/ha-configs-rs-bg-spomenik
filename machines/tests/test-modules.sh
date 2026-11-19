#!/usr/bin/env bash
# The login shell rules of the modules, each module run directly (DT_OPTIONS / DT_CONFIG as the engine sets them):
# the old shell keys fail with exit 2, login-shell rejects a shells value that is not a table and handles every entry
# when a user is missing; as root in a container also: users creates a user whose declared shell is not usable with
# nologin, login-shell sets the shell of the users that exist, and ssh-server counts only admins with a login shell.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

modules=$MACHINES/bananapi-m4/modules
# module NAME MODE OPTIONS [CONFIG]: run one module the way the engine does
module() {
	run env DT_DIR="$MACHINES/bananapi-m4" DT_OPTIONS="$3" DT_CONFIG="${4:-{\}}" "$modules/$1/run" "$2"
}

module users check '{"users": [{"name": "dt-old", "shell": "/bin/bash"}]}'
expect "users: shell in [[modules.users.users]] exits 2" test "$code" = 2
expect "  ... pointing at [modules.login-shell.shells]" contains "$err" "[modules.login-shell.shells]"
module dotfiles check '{"login_shell": "/usr/bin/zsh"}'
expect "dotfiles: login_shell exits 2" test "$code" = 2
expect "  ... pointing at [modules.login-shell.shells]" contains "$err" "[modules.login-shell.shells]"

for shells in '["/bin/bash"]' '"/bin/bash"' '{"root": 1}'; do
	module login-shell check "{\"shells\": $shells}"
	expect "login-shell: shells = $shells exits 2" test "$code" = 2
done

module login-shell plan '{"shells": {"dt-no-such-user": "/bin/sh", "root": "/nonexistent/sh"}}'
expect "login-shell plan: a missing user drifts (exit 3)" test "$code" = 3
expect "  ... naming it" contains "$out" "dt-no-such-user: login shell -> /bin/sh once the user exists"
expect "  ... and the next entry is still handled" contains "$out" "root: login shell"

if ((EUID != 0)) || [[ ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
	echo "skip the rest (root in a container only)"
	done_testing
	exit 0
fi

key="ssh-ed25519 AAAAtest-key-not-a-real-one dt@test"
names=(dt-later dt-admin dt-bash dt-plain dt-off dt-typo)
cleanup() {
	local name
	for name in "${names[@]}"; do
		userdel -r "$name" >/dev/null 2>&1 || true
		rm -f "/etc/sudoers.d/90-debian-tweaks-$name"
	done
}
trap 'cleanup; rm -rf "$WORK"' EXIT
cleanup
config='{"modules": {"login-shell": {"shells": {"dt-later": "/nonexistent/fish", "dt-admin": "/nonexistent/fish",
	"dt-bash": "/bin/bash"}}}}'
users="[{\"name\": \"dt-later\", \"authorized_keys\": [\"$key\"]},
	{\"name\": \"dt-admin\", \"sudo\": \"nopasswd\", \"authorized_keys\": [\"$key\"]},
	{\"name\": \"dt-bash\"}, {\"name\": \"dt-plain\"}]"
module users install "{\"users\": $users}" "$config"
expect "users install: creates the users (exit 0)" test "$code" = 0
shell_of() { getent passwd "$1" | cut -d: -f7; }
default=$(useradd -D | sed -n 's/^SHELL=//p')
expect "useradd's default shell is known ($default)" test -n "$default"
expect "  ... a declared shell that is not installed: nologin, not /bin/sh ($(shell_of dt-later))" \
	test "$(shell_of dt-later)" = /usr/sbin/nologin
expect "  ... the same for a nopasswd sudo user: useradd's default, it stays an admin ($(shell_of dt-admin))" \
	test "$(shell_of dt-admin)" = "$default"
expect "  ... a declared usable shell: that one ($(shell_of dt-bash))" test "$(shell_of dt-bash)" = /bin/bash
expect "  ... none declared: useradd's default ($(shell_of dt-plain))" test "$(shell_of dt-plain)" = "$default"

# ssh_config USER SUDO [SHELLS]: a machine.toml whose only admin candidate is USER; SHELLS: [modules.login-shell.shells]
ssh_config() {
	echo "{\"modules\": {\"users\": {\"users\": [{\"name\": \"$1\", \"sudo\": \"$2\", \"authorized_keys\": [\"$key\"]}]},
		\"login-shell\": {\"shells\": ${3:-{\}}}}}"
}
module ssh-server check '{"password_authentication": false}' "$(ssh_config dt-later nopasswd)"
expect "ssh-server: an admin whose shell is nologin does not count (exit 1)" test "$code" = 1
expect "  ... refusing" contains "$err" "refusing password_authentication = false"
module ssh-server check '{"password_authentication": false}' "$(ssh_config dt-bash nopasswd)"
expect "ssh-server: an admin with /bin/bash counts (exit 3: the drop-in is not there yet)" test "$code" = 3
expect "  ... no refusal" lacks "$err" "refusing"
usermod --shell /nonexistent/sh dt-bash
module ssh-server check '{"password_authentication": false}' "$(ssh_config dt-bash nopasswd)"
expect "ssh-server: an admin whose shell is gone does not count (exit 1)" test "$code" = 1
usermod --shell /bin/bash dt-bash
module ssh-server check '{"password_authentication": false}' \
	"$(ssh_config dt-bash nopasswd '{"dt-bash": "/bin/false"}')"
expect "ssh-server: an admin whose shell login-shell sets to /bin/false later does not count (exit 1)" test "$code" = 1
module ssh-server check '{"password_authentication": false}' \
	"$(ssh_config dt-later nopasswd '{"dt-later": "/bin/bash"}')"
expect "ssh-server: an admin on nologin today does not count, whatever login-shell sets later (exit 1)" test "$code" = 1
module ssh-server check '{"password_authentication": false}' "$(ssh_config dt-not-yet nopasswd)"
expect "ssh-server preview: a user not created yet counts by useradd's default shell (exit 3)" test "$code" = 3
off=$(ssh_config dt-not-yet nopasswd | jq -c '.modules.users.enabled = false')
module ssh-server check '{"password_authentication": false}' "$off"
expect "ssh-server preview: ... but not with the users module off (exit 1)" test "$code" = 1
module ssh-server install '{"password_authentication": false}' "$(ssh_config dt-not-yet nopasswd)"
expect "ssh-server install: a user that does not exist does not count (exit 1)" test "$code" = 1
expect "  ... refusing (not some later failure)" contains "$err" "refusing password_authentication = false"

module users install '{"users": [{"name": "dt-plain"}, {"name": "dt-bash", "sudo": "password"}]}' \
	'{"modules": {"login-shell": {"shells": {"dt-plain": "/nonexistent/git-shell", "dt-bash": "/nonexistent/fish"}}}}'
expect "users install: existing users whose declared shell is not usable yet (exit 0)" test "$code" = 0
expect "  ... keep their shell: it is login-shell's ($(shell_of dt-plain), $(shell_of dt-bash))" \
	test "$(shell_of dt-plain):$(shell_of dt-bash)" = "$default:/bin/bash"
module users install '{"users": [{"name": "dt-typo", "sudo": "nopaswd"}]}' \
	'{"modules": {"login-shell": {"shells": {"dt-typo": "/nonexistent/fish"}}}}'
expect "users install: a misspelt sudo exits 2" test "$code" = 2
expect "  ... before creating the user" bash -c '! getent passwd dt-typo'
module users install '{"users": [{"name": "dt-off"}]}' \
	'{"modules": {"login-shell": {"enabled": false, "shells": {"dt-off": "/nonexistent/fish"}}}}'
expect "users install: login-shell off, its shells are ignored ($(shell_of dt-off))" \
	test "$(shell_of dt-off)" = "$default"

module login-shell install '{"shells": {"dt-no-such-user": "/bin/sh", "dt-plain": "/bin/bash"}}'
expect "login-shell install: a missing user fails the module (exit 1)" test "$code" = 1
expect "  ... naming it" contains "$err" "no such user: dt-no-such-user"
expect "  ... after setting the next user's shell ($(shell_of dt-plain))" test "$(shell_of dt-plain)" = /bin/bash

done_testing
