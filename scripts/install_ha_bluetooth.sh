#!/usr/bin/env bash
# Run once as part of host provisioning. Boot requires no manual Bluetooth actions.
# Optional positional args are machine-specific Compose overlays, in merge order.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID -eq 0 ]] || { echo 'Run the host installer as root.' >&2; exit 2; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v docker >/dev/null || { echo 'Install Docker Engine and Docker Compose first.' >&2; exit 1; }
owner=$(stat -c '%U' "$ROOT")
[[ "$owner" != root ]] || { echo 'The HA checkout must belong to its service user, not root.' >&2; exit 1; }
backup="/var/backups/ha-bluetooth/$(date -u +%Y%m%dT%H%M%S%NZ)"
install -d -m 700 "$backup"
for f in /etc/default/ha-compose /etc/systemd/system/ha-compose.service; do
  [[ ! -f "$f" ]] || cp -a "$f" "$backup/$(basename "$f")"
done
# Preserve existing machine overlays and the HA image pin. Never store resolved
# Compose config here: it may contain environment secrets.
python3 - "$ROOT" "$@" <<'PY'
import json, pathlib, shlex, subprocess, sys
root = pathlib.Path(sys.argv[1])
overlays = sys.argv[2:]
if not overlays:
    p = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "com.docker.compose.project.config_files"}}', 'homeassistant-homeassistant-1'], capture_output=True, text=True)
    if p.returncode == 0:
        overlays = [x for x in p.stdout.strip().split(',') if x and pathlib.Path(x).name not in ('ha.docker-compose.yaml', 'compose.final.yaml')]
    elif pathlib.Path('/etc/default/ha-compose').exists():
        # Keep previously installed configuration when the container is absent.
        sys.exit(0)
for path in overlays:
    if not pathlib.Path(path).is_file():
        raise SystemExit(f'Missing Compose overlay: {path}')
pathlib.Path('/etc/default/ha-compose').write_text('# Local machine overlay paths; managed by install_ha_bluetooth.sh\nHA_COMPOSE_OVERLAYS=' + shlex.quote(':'.join(overlays)) + '\n')
PY
chmod 644 /etc/default/ha-compose
"$ROOT/scripts/ha_compose.sh" config --quiet
"$ROOT/scripts/ha_bluetooth_host.sh"
python3 - "$ROOT" "$owner" <<'PY'
from pathlib import Path
import sys
root, owner = sys.argv[1:]
s = (Path(root) / 'scripts/host/ha-compose.service').read_text()
s = s.replace('/home/ultra/homeassistant', root).replace('User=ultra', f'User={owner}')
Path('/etc/systemd/system/ha-compose.service').write_text(s)
PY
systemctl daemon-reload
systemctl enable ha-compose.service
printf 'Host provisioning complete. Preserved deployment settings; backups: %s\n' "$backup"
printf 'Apply with: %s/scripts/ha_compose.sh up -d ha-docker-dns homeassistant\n' "$ROOT"
