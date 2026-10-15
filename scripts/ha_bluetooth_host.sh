#!/usr/bin/env bash
# Idempotent host prerequisites. Called as root by systemd before Compose startup.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
if [[ $EUID -ne 0 ]]; then
  echo 'Bluetooth host preparation requires root (the systemd unit supplies it).' >&2
  exit 2
fi
missing=()
command -v bluetoothd >/dev/null || missing+=(bluez)
command -v bluetoothctl >/dev/null || missing+=(bluez)
command -v dbus-send >/dev/null || missing+=(dbus)
if ((${#missing[@]})); then
  command -v apt-get >/dev/null || { echo 'Install BlueZ and system D-Bus for this distribution.' >&2; exit 1; }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends "${missing[@]}"
fi
systemctl start dbus.service
systemctl enable --now bluetooth.service
[[ -S /run/dbus/system_bus_socket ]] || { echo 'System D-Bus socket is missing.' >&2; exit 1; }
systemctl is-active --quiet dbus.service || { echo 'System D-Bus is not active.' >&2; exit 1; }
systemctl is-active --quiet bluetooth.service || { echo 'BlueZ is not active.' >&2; exit 1; }
# Do not restart BlueZ, reset adapters, or interrupt existing Bluetooth connections.
if command -v rfkill >/dev/null && ! rfkill unblock bluetooth; then
  echo 'Bluetooth rfkill unblock failed; continuing because adapters are optional.' >&2
fi
# Read the complete result before matching. With pipefail, grep -q can close
# early while bluetoothctl still writes discovery events and cause false absence.
if ! controllers=$(bluetoothctl --timeout 8 list); then
  echo 'Bluetooth adapter discovery failed; continuing because adapters are optional.' >&2
  exit 0
fi
if [[ "$controllers" == *"Controller "* ]]; then
  if ! bluetoothctl --timeout 8 power on; then
    echo 'Bluetooth adapter could not be powered on; continuing because adapters are optional.' >&2
  fi
else
  echo 'No Bluetooth adapter detected yet; BlueZ AutoEnable will handle hotplug.' >&2
fi
