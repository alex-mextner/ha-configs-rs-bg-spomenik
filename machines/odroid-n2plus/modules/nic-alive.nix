# NixOS port of /usr/local/sbin/odroid-nic-alive from the Armbian install.
#
# The on-board Ethernet (stmmac / meson8b-dwmac + RTL8211F PHY) has been seen to wedge: carrier
# stays up but RX stops. The kernel keeps feeding the hardware watchdog, so nothing recovers on
# its own. This check reboots when end0 has carrier but its RX byte counter has not moved for
# 10 minutes. The home LAN always carries broadcast traffic (ARP/mDNS), so a frozen counter with
# link up means the NIC is stuck. No carrier (cable out, router off) never triggers a reboot.
{ pkgs, ... }:

let
  check = pkgs.writeShellScript "odroid-nic-alive" ''
    IF=end0
    STALE=600
    STATE=/run/odroid-nic-alive

    if [ "$(cat "/sys/class/net/$IF/carrier" 2>/dev/null)" != 1 ]; then
      rm -f "$STATE"
      exit 0
    fi
    rx=$(cat "/sys/class/net/$IF/statistics/rx_bytes")
    now=$(cut -d. -f1 /proc/uptime)
    last_rx= last_t=$now
    [ -f "$STATE" ] && read -r last_rx last_t < "$STATE"
    if [ "$rx" != "$last_rx" ]; then
      echo "$rx $now" > "$STATE"
      exit 0
    fi
    [ $((now - last_t)) -lt "$STALE" ] && exit 0
    echo "$IF has carrier but no RX for $((now - last_t))s, rebooting" >&2
    exit 1
  '';
in
{
  systemd.services.odroid-nic-alive = {
    description = "Reboot when end0 has carrier but RX is frozen";
    unitConfig.FailureAction = "reboot";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = check;
    };
  };

  systemd.timers.odroid-nic-alive = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "1min";
    };
  };
}
