#!/usr/bin/env bash
# Register one runner declared in machine.toml ([[modules.actions-runners.runners]]) with GitHub and start its
# service. The one-hour registration token comes on stdin, never as an argument (every local user can read argv):
#
#   gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token |
#     ssh ultra@bananapi-m4 'cd debian-tweaks && sudo scripts/register-runner.sh <name>'
#
# Run `sudo ./install.sh --only actions-runners --commit` first: it unpacks the pinned runner release into the
# runner's directory. `--replace` takes over a runner of the same name already registered for the repository.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name=${1:?usage: register-runner.sh <runner name from machine.toml> (token on stdin)}
((EUID == 0)) || { echo "register-runner.sh: run as root (sudo)" >&2; exit 6; }

entry=$(python3 -c '
import json, sys, tomllib
with open(sys.argv[1], "rb") as f:
    runners = tomllib.load(f).get("modules", {}).get("actions-runners", {}).get("runners", [])
match = [r for r in runners if r.get("name") == sys.argv[2]]
print(json.dumps(match[0]) if match else "")
' "$here/machine.toml" "$name")
[[ -n $entry ]] || { echo "register-runner.sh: no runner named $name in machine.toml" >&2; exit 2; }
repo=$(jq -r '.repo' <<<"$entry")
user=$(jq -r '.user' <<<"$entry")
dir=$(jq -r '.dir' <<<"$entry")
labels=$(jq -r '.labels | join(",")' <<<"$entry")
[[ -x $dir/config.sh ]] || { echo "register-runner.sh: $dir has no runner yet: install.sh --only actions-runners --commit" >&2; exit 1; }

IFS= read -r token || true
[[ -n $token ]] || { echo "register-runner.sh: no token on stdin" >&2; exit 2; }
[[ ! -L $dir ]] || { echo "register-runner.sh: $dir is a symlink: refusing" >&2; exit 1; }
cd "$dir"
# ACTIONS_RUNNER_INPUT_TOKEN is config.sh's environment form of --token; runuser keeps the environment.
ACTIONS_RUNNER_INPUT_TOKEN=$token runuser -u "$user" -- ./config.sh --unattended --replace \
	--url "https://github.com/$repo" --name "$name" --labels "$labels" --work _work
# The unit comes from machine.toml, never from the runner directory's svc.sh (the runner user can rewrite it). The
# module exits 1 while other declared runners are still unregistered, so success is this runner's own unit running.
"$here/install.sh" --only actions-runners --commit || true
svc=actions.runner.${repo/\//-}.$name.service
systemctl is-active -q "$svc" || { echo "register-runner.sh: $svc is not running" >&2; exit 1; }
echo "register-runner.sh: $name registered, $svc running"
