#!/usr/bin/env bash
# Ctrl-C and SIGTERM to install.sh reach the dotfiles installer, which runs as the user in a session of its own: the
# installer and its children are gone when install.sh exits 130 / 143. Needs root and creates a user, so it runs only
# in a container (`make test-machines`); the dotfiles module's packages must be installed already (no apt here).
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

if ((EUID != 0)) || [[ ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
	echo "skip (root in a container only)"
	exit 0
fi

user=dt-signals
id "$user" >/dev/null 2>&1 || useradd --create-home --user-group --shell /bin/bash "$user"
home=$(getent passwd "$user" | cut -d: -f6)
fake=$WORK/fake
git init -q -b master "$fake"
cat >"$fake/install-ubuntu.sh" <<'EOF'
#!/usr/bin/env bash
# stand-in for .files' installer: pending until run, then sleeps; records its pids and the TERM it gets
[[ ${1:-} == --check ]] && exit 1
log=$HOME/installer.log
trap 'echo "got TERM" >>"$log"; exit 143' TERM
echo "$$" >>"$log"
sleep 600 &
echo "$!" >>"$log"
wait
EOF
chmod +x "$fake/install-ubuntu.sh"
git -C "$fake" add install-ubuntu.sh
git -C "$fake" -c user.name=t -c user.email=t@t.invalid commit -qm fake
rev=$(git -C "$fake" rev-parse HEAD)
chmod a+rx "$WORK"
chown -R "$user:" "$fake" # the user clones it: git refuses a repository someone else owns
machine=$WORK/machine.toml
{
	for name in "$MACHINES"/bananapi-m4/modules/*/; do
		name=$(basename "$name")
		[[ $name == dotfiles || $name == system ]] || printf '[modules.%s]\nenabled = false\n' "$name"
	done
	printf '[modules.system]\nenabled = false\nhostname = "%s"\n' "$(hostname)"
	printf '[modules.dotfiles]\nuser = "%s"\nrepo = "%s"\nrev = "%s"\n' "$user" "$fake" "$rev"
} >"$machine"

for how in INT TERM; do
	rm -rf "$home/.files" "$home/installer.log"
	set -m # install.sh in a process group of its own, as a terminal runs it
	"$MACHINES/bananapi-m4/install.sh" --machine "$machine" --only dotfiles --commit >"$WORK/engine.log" 2>&1 &
	engine=$!
	set +m
	for _ in $(seq 150); do
		[[ $(cat "$home/installer.log" 2>/dev/null | wc -l) -ge 2 ]] && break
		sleep 0.2
	done
	mapfile -t pids < <(cat "$home/installer.log" 2>/dev/null)
	if [[ $how == INT ]]; then kill -INT -- "-$engine" || true; else kill -TERM "$engine" || true; fi
	code=0
	wait "$engine" || code=$?
	out=$(<"$WORK/engine.log") err=''
	expected=$([[ $how == INT ]] && echo 130 || echo 143)
	expect "SIG$how: the installer was running (pids ${pids[*]:-none})" test "${#pids[@]}" -ge 2
	expect "  ... install.sh exits $expected" test "$code" = "$expected"
	expect "  ... the installer got SIGTERM" grep -qx "got TERM" "$home/installer.log"
	gone() { local pid; for pid in "${pids[@]}"; do [[ ! -e /proc/$pid ]] || return 1; done; }
	expect "  ... the installer and its sleep are gone" gone
	expect "  ... the module reports it" contains "$out" "install-ubuntu.sh stopped on SIG$how"
done

done_testing
