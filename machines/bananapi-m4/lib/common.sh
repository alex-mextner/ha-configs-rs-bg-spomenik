# shellcheck shell=bash
# Shared helpers for modules/*/run. One code path serves all three verbs, so what `plan` shows is what `install`
# does and what `check` measures:
#   check    read-only, silent; exit 0 when nothing drifts, 3 otherwise
#   plan     read-only; prints every change (unified diff for files); exit 0 or 3
#   install  makes each drifting item true (root); any failing command fails the module
set -euo pipefail

MODE=${1:-}
case "$MODE" in check | plan | install) ;; *)
	echo "usage: $0 check|plan|install" >&2
	exit 2
	;;
esac
DRIFT=0
# shellcheck disable=SC2034 # used by the modules that source this file
MARKER="# Managed by debian-tweaks ($(basename "$(dirname "$0")")): edit machine.toml, not this file."
BACKUP_DIR=/var/backups/debian-tweaks

# stop_on_signals: SIGINT/SIGTERM (the engine passes them on) end the module only once the command it is running has
# finished (bash runs a trap after the foreground command), so apt or dpkg is never left running on its own
stop_on_signals() {
	trap 'exit 130' INT
	trap 'exit 143' TERM
}
stop_on_signals

# opt JQ_FILTER: read this module's options (DT_OPTIONS); `jq -r`, so a missing key prints "null".
opt() { jq -r "$1" <<<"${DT_OPTIONS:-{\}}"; }
# cfg JQ_FILTER: read the whole machine.toml (DT_CONFIG).
cfg() { jq -r "$1" <<<"${DT_CONFIG:-{\}}"; }

say() { [[ $MODE == check ]] || echo "  $*"; }

# drift DESCRIPTION: one item is not as declared; returns 0 only in install mode (so callers can `drift ... && fix`).
drift() {
	DRIFT=$((DRIFT + 1))
	say "~ $*"
	[[ $MODE == install ]]
}

# ensure_file PATH MODE OWNER:GROUP < content
# Returns 0 when the file was (or, in plan/check, would be) changed, 1 when it already matches.
# A file not written by debian-tweaks is copied to $BACKUP_DIR before it is replaced.
ensure_file() {
	local path=$1 mode=$2 owner=$3 tmp
	tmp=$(mktemp)
	cat >"$tmp"
	if [[ -f $path ]] && cmp -s "$tmp" "$path" && [[ $(stat -c '%a %U:%G' "$path") == "$mode $owner" ]]; then
		rm -f "$tmp"
		return 1
	fi
	DRIFT=$((DRIFT + 1))
	case "$MODE" in
	plan)
		if [[ -f $path ]]; then
			echo "  ~ $path ($(stat -c '%a %U:%G' "$path") -> $mode $owner)"
			diff -u "$path" "$tmp" | tail -n +3 | sed 's/^/      /' || true
		else
			echo "  + $path ($mode $owner)"
			sed 's/^/      /' "$tmp"
		fi
		;;
	install)
		if [[ -s $path ]] && ! grep -qF "# Managed by debian-tweaks" "$path"; then
			install -d -m 700 "$BACKUP_DIR"
			cp -a "$path" "$BACKUP_DIR/$(echo "${path#/}" | tr / _).$(date +%Y%m%dT%H%M%S)"
		fi
		install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$path"
		echo "  wrote $path"
		;;
	esac
	rm -f "$tmp"
	return 0
}

# absent PATH: a file debian-tweaks once wrote must go (only removed when it carries the marker).
absent() {
	local path=$1
	[[ -f $path ]] && grep -qF "# Managed by debian-tweaks" "$path" || return 1
	drift "remove $path" && rm -f "$path"
	return 0
}

# shell_usable PATH: PATH may become a user's shell: an executable listed in /etc/shells, or a non-login shell
# (nologin, false: never listed there). sshd refuses a user whose shell is missing, so setting one before its package
# is installed locks the user out of this headless box.
shell_usable() {
	[[ -f $1 && -x $1 ]] || return 1
	case ${1##*/} in nologin | false) return 0 ;; esac
	grep -qxF -- "$1" /etc/shells 2>/dev/null
}

finish() {
	[[ $MODE == install ]] && exit 0
	((DRIFT == 0)) || exit 3
	exit 0
}
