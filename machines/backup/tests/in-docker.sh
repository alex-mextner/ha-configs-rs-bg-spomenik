#!/usr/bin/env bash
# tests/roundtrip.sh and tests/module.sh as root in a throwaway Debian trixie container (the hosts' distribution):
# the SQLite dumps of databases that belong to another user, as on the system hosts, and the scope = "system"
# install of debian/run. Builds the image homelab-backup-test:trixie from tests/Dockerfile (cached after the first
# build); the pinned static restic and rclone come from $HOMELAB_BACKUP_TEST_BIN (default ~/.local/bin, where the
# module puts them).
#   machines/backup/tests/in-docker.sh        exit 0 = both suites passed
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
machines=$(cd "$here/../.." && pwd)
bin=${HOMELAB_BACKUP_TEST_BIN:-$HOME/.local/bin}
image=homelab-backup-test:trixie
for tool in restic rclone; do
	[[ -x $bin/$tool ]] || { echo "in-docker.sh: no $bin/$tool (machines/backup/debian/install.sh puts it there)" >&2; exit 2; }
done
# the build log only on failure: the legacy builder warns on every build
if ! log=$(docker build -q -t "$image" "$here" 2>&1); then
	echo "$log" >&2
	exit 1
fi
# --mount, not -v: a missing source fails instead of becoming a root-owned directory on the host
docker run --rm \
	--mount "type=bind,source=$machines,target=/src/machines,readonly" \
	--mount "type=bind,source=$bin/restic,target=/opt/bin/restic,readonly" \
	--mount "type=bind,source=$bin/rclone,target=/opt/bin/rclone,readonly" \
	-e HOMELAB_BACKUP_TEST_BIN=/opt/bin -e HOMELAB_BACKUP_TEST_CONTAINER=1 \
	"$image" bash -c '/src/machines/backup/tests/roundtrip.sh && /src/machines/backup/tests/module.sh'
