#!/usr/bin/env bash
# Ctrl-C and SIGTERM to install.sh reach the dotfiles installer, which runs as the user in a session of its own: the
# installer and its children are gone when install.sh exits 130 / 143, also when the installer ignores SIGTERM (it is
# SIGKILLed 30 s later). Needs root and creates a user, so it runs only in a container (`make test-machines`); the
# dotfiles module's packages must be installed already (no apt here).
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
# stand-in for .files' installer: pending until run, then sleeps; records its pids and the TERM it gets. With
# ~/ignore-term it only logs TERM and keeps going; with ~/orphan its sleep ignores TERM, so it outlives the installer.
[[ ${1:-} == --check ]] && exit 1
log=$HOME/installer.log
if [[ -e $HOME/ignore-term ]]; then
	trap 'echo "got TERM" >>"$log"' TERM
	echo "$$" >>"$log"
	sleep 600 &
	echo "$!" >>"$log"
	while :; do sleep 1; done
fi
trap 'echo "got TERM" >>"$log"; exit 143' TERM
echo "$$" >>"$log"
# the orphan logs its pid itself, once it ignores TERM
if [[ -e $HOME/orphan ]]; then
	bash -c 'trap "" TERM; echo "$$" >>"$1"; exec sleep 600' _ "$log" &
else
	sleep 600 &
	echo "$!" >>"$log"
fi
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

pids=()
gone() { local pid; for pid in "${pids[@]}"; do [[ ! -e /proc/$pid ]] || return 1; done; }
left() { pgrep -u "$user" >/dev/null; test $? = 1; } # 1: no process; pgrep missing or failing is not a pass
for how in INT TERM ignored-TERM orphan-TERM; do
	rm -rf "$home/.files" "$home/installer.log" "$home/ignore-term" "$home/orphan"
	[[ $how != ignored-TERM ]] || touch "$home/ignore-term"
	[[ $how != orphan-TERM ]] || touch "$home/orphan"
	set -m # install.sh in a process group of its own, as a terminal runs it
	"$MACHINES/bananapi-m4/install.sh" --machine "$machine" --only dotfiles --commit >"$WORK/engine.log" 2>&1 &
	engine=$!
	set +m
	for _ in $(seq 150); do
		[[ $(cat "$home/installer.log" 2>/dev/null | wc -l) -ge 2 ]] && break
		sleep 0.2
	done
	mapfile -t pids < <(cat "$home/installer.log" 2>/dev/null)
	start=$SECONDS
	if [[ $how == INT ]]; then kill -INT -- "-$engine" || true; else kill -TERM "$engine" || true; fi
	code=0
	wait "$engine" || code=$?
	took=$((SECONDS - start))
	out=$(<"$WORK/engine.log") err=''
	expected=$([[ $how == INT ]] && echo 130 || echo 143)
	signal=${how##*-}
	expect "$how: the installer was running (pids ${pids[*]:-none})" test "${#pids[@]}" -ge 2
	expect "  ... install.sh exits $expected" test "$code" = "$expected"
	expect "  ... the installer got SIGTERM" grep -qx "got TERM" "$home/installer.log"
	expect "  ... the installer and its sleep are gone" gone
	expect "  ... the module reports it" contains "$out" "install-ubuntu.sh stopped on SIG$signal"
	if [[ $how == ignored-TERM ]]; then
		expect "  ... after the 30 s grace, not before (${took} s)" test "$took" -ge 29 -a "$took" -le 40
	fi
	expect "  ... no process of $user is left" left
done

done_testing
