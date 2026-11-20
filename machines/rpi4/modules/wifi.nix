# Wi-Fi: the SSID is here, the password only in the sops secret wifi-env.
{ config, pkgs, ... }:
{
  # Decrypted content of wifi-env:  WIFI_PSK=<password>
  # wpa_supplicant's ext_password_backend=file reads NAME=value lines; the value is everything
  # after the first "=" up to the end of the line ("#" is allowed, only a line STARTING with "#" is
  # a comment), the passphrase is 8..63 characters (or 64 hex).
  sops.secrets.wifi-env = {
    # wpa_supplicant runs as this user (enableHardening)
    owner = "wpa_supplicant";
    # wpa_supplicant keeps the secret file open: a new password needs a restart
    restartUnits = [ "wpa_supplicant.service" ];
  };

  networking.wireless = {
    enable = true;
    secretsFile = config.sops.secrets.wifi-env.path;
    networks."moscow".pskRaw = "ext:WIFI_PSK";
  };

  # The Broadcom Wi-Fi chip of the Pi 4 dozes off when idle: keep it awake. The unit is bound to
  # the device: it starts when wlan0 appears (also after a driver reload) and stops with it.
  systemd.services.wifi-powersave-off = {
    description = "Disable Wi-Fi power saving on wlan0";
    bindsTo = [ "sys-subsystem-net-devices-wlan0.device" ];
    wantedBy = [ "sys-subsystem-net-devices-wlan0.device" ];
    after = [
      "sys-subsystem-net-devices-wlan0.device"
      "wpa_supplicant.service"
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.iw}/bin/iw dev wlan0 set power_save off";
    };
  };
}
