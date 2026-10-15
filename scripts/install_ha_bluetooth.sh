#!/usr/bin/env bash
# Host provisioning: stage checked deployment settings, then atomically publish them.
# The systemd unit may execute as root, so its Bluetooth helper is copied from this
# reviewed checkout into a root-owned path before the unit is enabled.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID -eq 0 ]] || { echo 'Run the host installer as root.' >&2; exit 2; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
case "$ROOT" in
  *[[:space:]%]*)
    echo 'The HA checkout path cannot contain whitespace or % for systemd.' >&2
    exit 2
    ;;
esac
command -v docker >/dev/null || { echo 'Install Docker Engine and Docker Compose first.' >&2; exit 1; }
owner=$(stat -c '%U' "$ROOT")
[[ "$owner" != root ]] || { echo 'The HA checkout must belong to its service user, not root.' >&2; exit 1; }
[[ "$owner" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || {
  echo 'The HA checkout owner is not safe for a systemd User= field.' >&2
  exit 2
}

SETTINGS=/etc/default/ha-compose
UNIT=/etc/systemd/system/ha-compose.service
HELPER=/usr/local/libexec/ha-bluetooth-host
settings_candidate=$(mktemp "$(dirname "$SETTINGS")/.ha-compose.XXXXXX")
unit_candidate=$(mktemp "$(dirname "$UNIT")/.ha-compose.service.XXXXXX")
published=0
backup=
was_enabled=0

cleanup() {
  local status=$?
  if ((published)); then
    echo 'Activation failed; restoring previous Bluetooth migration files.' >&2
    restore_target "$SETTINGS" settings
    restore_target "$UNIT" unit
    restore_target "$HELPER" helper
    systemctl daemon-reload || true
    if ((was_enabled)); then
      systemctl enable ha-compose.service || true
    else
      systemctl disable ha-compose.service || true
    fi
  fi
  rm -f "$settings_candidate" "$unit_candidate"
  exit "$status"
}
trap cleanup EXIT

snapshot_target() {
  local target=$1 name=$2
  if [[ -e "$target" ]]; then
    cp -a "$target" "$backup/$name"
    : >"$backup/$name.present"
  else
    : >"$backup/$name.absent"
  fi
}

restore_target() {
  local target=$1 name=$2
  if [[ -e "$backup/$name.present" ]]; then
    cp -a "$backup/$name" "$target"
  else
    rm -f "$target"
  fi
}

# Preserve a valid existing file verbatim when no explicit overlays are supplied.
# The parser permits only the two variables consumed by ha_compose.sh, so validating
# the candidate cannot execute settings controlled by an untrusted file.
python3 - "$ROOT" "$SETTINGS" "$settings_candidate" "$@" <<'PY'
import os
import pathlib
import re
import shlex
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
settings = pathlib.Path(sys.argv[2])
candidate = pathlib.Path(sys.argv[3])
explicit_overlays = sys.argv[4:]
allowed = {"HA_COMPOSE_OVERLAYS", "HA_COMPOSE_PROJECT_NAME"}


def parse_settings(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", stripped)
        if match is None or match.group(1) not in allowed:
            raise SystemExit(
                f"Unsafe Compose settings line {number}: only {', '.join(sorted(allowed))} are allowed"
            )
        key, raw_value = match.groups()
        if any(character in raw_value for character in "$`;&|<>"):
            raise SystemExit(f"Unsafe Compose settings line {number}: shell operators are not allowed")
        if any(character.isspace() for character in raw_value) and not (
            len(raw_value) >= 2
            and raw_value[0] in "\"'"
            and raw_value[-1] == raw_value[0]
        ):
            raise SystemExit(f"Unsafe Compose settings line {number}: quote whitespace")
        try:
            words = shlex.split(raw_value, posix=True, comments=False)
            if len(words) != 1:
                raise ValueError("settings must contain exactly one shell word")
            values[key] = words[0]
        except (IndexError, ValueError) as error:
            raise SystemExit(f"Invalid Compose settings line {number}") from error
    return values


def absolute_overlay(raw_path: str) -> str:
    path = pathlib.Path(raw_path).resolve(strict=True)
    if not path.is_file():
        raise SystemExit(f"Missing Compose overlay: {raw_path}")
    if ":" in str(path):
        raise SystemExit(f"Compose overlay paths cannot contain ':': {path}")
    return str(path)


existing = parse_settings(settings) if settings.exists() else {}
if settings.exists() and not explicit_overlays:
    candidate.write_bytes(settings.read_bytes())
    raise SystemExit(0)

project_name = existing.get("HA_COMPOSE_PROJECT_NAME") or os.environ.get(
    "HA_COMPOSE_PROJECT_NAME", "homeassistant"
)
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project_name):
    raise SystemExit("HA_COMPOSE_PROJECT_NAME contains unsupported characters")

overlays = [absolute_overlay(path) for path in explicit_overlays]
if not overlays:
    container = f"{project_name}-homeassistant-1"
    inspection = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
            container,
        ],
        capture_output=True,
        text=True,
    )
    label = inspection.stdout.strip()
    if inspection.returncode == 0 and label not in {"", "<no value>"}:
        overlays = [
            absolute_overlay(path)
            for path in label.split(",")
            if pathlib.Path(path).name not in {"ha.docker-compose.yaml", "compose.final.yaml"}
        ]

lines = ["# Local machine overlay paths; managed by install_ha_bluetooth.sh"]
if project_name != "homeassistant" or "HA_COMPOSE_PROJECT_NAME" in existing:
    lines.append(f"HA_COMPOSE_PROJECT_NAME={shlex.quote(project_name)}")
if overlays:
    lines.append(f"HA_COMPOSE_OVERLAYS={shlex.quote(':'.join(overlays))}")
candidate.write_text("\n".join(lines) + "\n")
PY

HA_COMPOSE_ENV_FILE="$settings_candidate" "$ROOT/scripts/ha_compose.sh" config --quiet
"$ROOT/scripts/ha_bluetooth_host.sh"
python3 - "$ROOT" "$owner" "$HELPER" "$unit_candidate" <<'PY'
from pathlib import Path
import sys

root, owner, helper, output = sys.argv[1:]
template = (Path(root) / "scripts/host/ha-compose.service").read_text()
tokens = {"@HA_ROOT@": root, "@HA_USER@": owner, "@HA_BLUETOOTH_HELPER@": helper}
expected_token_counts = {"@HA_ROOT@": 4, "@HA_USER@": 1, "@HA_BLUETOOTH_HELPER@": 1}
for token, expected_count in expected_token_counts.items():
    if template.count(token) != expected_count:
        raise SystemExit(f"Invalid unit template: expected {expected_count} {token}")
if "ExecStartPre=+@HA_BLUETOOTH_HELPER@" not in template:
    raise SystemExit("Invalid unit template: ExecStartPre must use the immutable helper")
if "User=@HA_USER@" not in template:
    raise SystemExit("Invalid unit template: User field is missing")
for token, value in tokens.items():
    template = template.replace(token, value)
if "@HA_" in template:
    raise SystemExit("Invalid unit template: unresolved token")
Path(output).write_text(template)
PY

backup="/var/backups/ha-bluetooth/$(date -u +%Y%m%dT%H%M%S%NZ)"
install -d -m 700 "$backup"
snapshot_target "$SETTINGS" settings
snapshot_target "$UNIT" unit
snapshot_target "$HELPER" helper
if systemctl is-enabled --quiet ha-compose.service; then was_enabled=1; fi

# Minimal hosts may not have libexec yet. Never rely on a hand-created directory.
if [[ ! -d "$(dirname "$HELPER")" ]]; then
  install -d -o root -g root -m 755 "$(dirname "$HELPER")"
fi
published=1
if [[ $# -gt 0 || ! -e "$SETTINGS" ]]; then
  install -m 644 "$settings_candidate" "$SETTINGS"
fi
install -o root -g root -m 755 "$ROOT/scripts/ha_bluetooth_host.sh" "$HELPER"
install -o root -g root -m 644 "$unit_candidate" "$UNIT"
systemctl daemon-reload
systemctl enable ha-compose.service
published=0
trap - EXIT
rm -f "$settings_candidate" "$unit_candidate"
printf 'Host provisioning complete. Preserved deployment settings; backups: %s\n' "$backup"
printf 'Apply with: %s/scripts/ha_compose.sh up -d ha-docker-dns homeassistant\n' "$ROOT"
