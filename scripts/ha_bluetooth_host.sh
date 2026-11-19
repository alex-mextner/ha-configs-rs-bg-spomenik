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
# Do not restart BlueZ, reset adapters, or interrupt existing Bluetooth connections.
# rfkill before the BlueZ check: unblocking by type also sets the default for an adapter that
# appears later, and the no-adapter path below exits early.
if command -v rfkill >/dev/null && ! rfkill unblock bluetooth; then
  echo 'Bluetooth rfkill unblock failed; continuing because adapters are optional.' >&2
fi
if ! systemctl is-active --quiet bluetooth.service; then
  # bluetooth.service has ConditionPathIsDirectory=/sys/class/bluetooth: until a USB adapter
  # enumerates (late at boot, or unplugged) the start is skipped, not failed, and the
  # adapter's udev bluetooth.target starts BlueZ on hotplug. Adapters are optional, so only
  # a BlueZ that was allowed to start and did not is an error. That hotplug start can land
  # right after the check above, so both properties come from one `show` snapshot: a separate
  # ConditionResult query could pair "inactive" with the newer start's "yes".
  bluez=$(systemctl show -p ActiveState -p ConditionResult bluetooth.service)
  if grep -qx 'ConditionResult=no' <<<"$bluez" \
    && ! grep -qxE 'ActiveState=(active|activating|reloading)' <<<"$bluez"; then
    echo 'No Bluetooth adapter yet (bluetooth.service condition unmet); BlueZ starts on adapter hotplug.' >&2
    exit 0
  fi
  # BlueZ was allowed to start. `start` joins a start still in flight (that hotplug's) and
  # returns its outcome; BlueZ is Type=dbus, so "activating" alone does not mean it will run.
  if ! systemctl start bluetooth.service || ! systemctl is-active --quiet bluetooth.service; then
    echo 'BlueZ is not active.' >&2
    exit 1
  fi
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
