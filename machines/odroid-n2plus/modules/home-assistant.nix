# Home Assistant and its sidecars. The containers stay Docker Compose, defined by the HA config checkout at
# /home/ultra/homeassistant (this repository, on the board's eMMC): ha.docker-compose.yaml + the 3d-cli include +
# ../ha/compose.odroid.yaml (this board's overlay, installed under /etc) + docker/bluetooth/compose.final.yaml,
# always through the checkout's scripts/ha_compose.sh. Services: homeassistant (host network), music-assistant,
# esphome, hga-postgres (pgvector), go2rtc, u1-camera-proxy, printer-proxy, avahi-reflector, ha-docker-dns
# (the CoreDNS of ha-net), bgutil-pot, ssh-server.
#
# Host side: BlueZ for HA's Bluetooth, the host action bridge (Kodi, DHCP pins, router inventory), the mDNS
# publisher (homeassistant.local), the soil probe ADC bridge, and the fail-safe checks that
# were ultra's crontab on Armbian.
#
# avahi stays the avahi-reflector container: it relays only three service types between the LAN and the ha-net
# bridge through its own macvlan leg (192.168.0.5); a host avahi-daemon would reflect between every interface and
# compete for UDP 5353 with HA, Music Assistant and the publisher below, which all speak mDNS on the host.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  checkout = "/home/ultra/homeassistant";
  overlay = "/etc/ha-compose/compose.odroid.yaml";
  docker = config.virtualisation.docker.package;

  # The Armbian unit ran /usr/local/libexec/ha-bluetooth-host first; that script pins a Debian PATH and installs
  # BlueZ with apt when it is missing. Here BlueZ is declared below, so only the best-effort power-on remains.
  bluetoothPowerOn = pkgs.writeShellScript "ha-bluetooth-power-on" ''
    ${pkgs.util-linux}/bin/rfkill unblock bluetooth || true
    ${pkgs.bluez}/bin/bluetoothctl --timeout 8 power on || \
      echo "No Bluetooth adapter powered on; BlueZ AutoEnable handles hotplug." >&2
  '';

  mdnsPython = pkgs.python3.withPackages (ps: [ ps.zeroconf ]);
  soilPython = pkgs.python3.withPackages (ps: [ ps.libgpiod ]);

  # tools the checkout's shell/python helpers call
  haTools = [
    docker
    # scripts/*.sh start with `#!/usr/bin/env bash`; a NixOS unit has no bash on PATH unless listed here
    pkgs.bash
    pkgs.git
    pkgs.python3
    pkgs.curl
    pkgs.coreutils
    pkgs.findutils
    pkgs.gnugrep
    pkgs.gnused
    pkgs.gawk
    pkgs.procps
  ];

  # A oneshot as ultra on a timer (ultra's Armbian crontab).
  ultraCheck = description: command: {
    inherit description;
    after = [ "ha-compose.service" ];
    path = haTools;
    serviceConfig = {
      Type = "oneshot";
      User = "ultra";
      Group = "ultra";
      ExecStart = command;
      TimeoutStartSec = "4min";
      MemoryMax = "256M";
    };
  };
in
{
  virtualisation.docker = {
    enable = true;
    daemon.settings = {
      log-driver = "json-file";
      log-opts = {
        max-size = "10m";
        max-file = "3";
      };
      # containers keep running while dockerd restarts (a rebuild that changes Docker)
      live-restore = true;
    };
  };

  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };

  environment.etc."ha-compose/compose.odroid.yaml".source = ../ha/compose.odroid.yaml;
  # Read (sourced) by scripts/ha_compose.sh: deployment settings, never credentials.
  environment.etc."default/ha-compose".text = ''
    HA_COMPOSE_OVERLAYS=${overlay}
  '';

  systemd.services.ha-compose = {
    description = "Home Assistant Docker Compose stack";
    wantedBy = [ "multi-user.target" ];
    # docker is wanted, not required: a rebuild that restarts dockerd must not stop the stack (live-restore keeps
    # the containers running)
    wants = [
      "docker.service"
      "network-online.target"
      "bluetooth.service"
    ];
    after = [
      "docker.service"
      "network-online.target"
      "bluetooth.service"
    ];
    path = haTools;
    # A changed overlay or unit is applied with `up -d` (only the changed containers are recreated); a rebuild
    # never stops the whole stack.
    reloadIfChanged = true;
    # with reloadIfChanged a trigger change also becomes a reload, not a restart
    restartTriggers = [ config.environment.etc."ha-compose/compose.odroid.yaml".source ];
    unitConfig = {
      # Recover cold-boot DNS health races (ha-docker-dns) without an unlimited restart loop; the window is finite
      # so manual restarts later in the boot are not refused.
      StartLimitIntervalSec = "10min";
      StartLimitBurst = 5;
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      Restart = "on-failure";
      RestartSec = "30s";
      User = "ultra";
      Group = "ultra";
      WorkingDirectory = checkout;
      ExecStartPre = "+${bluetoothPowerOn}";
      ExecStart = "${checkout}/scripts/ha_compose.sh up -d";
      ExecReload = "${checkout}/scripts/ha_compose.sh up -d";
      ExecStop = "${checkout}/scripts/ha_compose.sh stop";
      TimeoutStartSec = "15min"; # first start after the move may pull or build images
      TimeoutStopSec = "180s";
      # the compose client only; the containers are in docker.slice (memory.nix)
      MemoryMax = "256M";
    };
  };

  # Restore/reconcile native Version + Template Update entries after HA starts.
  # HA_TOKEN stays in ~/.env, never in the Nix store. Runtime stores are read-only;
  # their IDs are checked against live metadata before any native config flow.
  systemd.services.ha-core-update-notice = {
    description = "Home Assistant Core update notice";
    wantedBy = [ "multi-user.target" ];
    wants = [ "ha-compose.service" ];
    after = [ "ha-compose.service" ];
    unitConfig = {
      ConditionPathExists = "/home/ultra/.env";
      StartLimitIntervalSec = "15min";
      StartLimitBurst = 3;
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = "ultra";
      Group = "ultra";
      ExecStart = "${pkgs.python3}/bin/python3 ${../../../scripts/provision_ha_core_update.py}";
      RuntimeDirectory = "ha-core-update-notice";
      RuntimeDirectoryMode = "0700";
      UMask = "0077";
      TimeoutStartSec = "5min";
      Restart = "on-failure";
      RestartSec = "30s";
      MemoryMax = "128M";
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only";
      ReadWritePaths = [ "/tmp" ];
      RestrictAddressFamilies = [ "AF_INET" "AF_UNIX" ];
    };
  };

  # Bridge between HA and this host (Kodi unit, Transmission and Kodi JSON-RPC, DHCP pins in the home-lan dnsmasq,
  # router inventory). It binds 172.20.0.1, the host side of ha-net, which exists only once compose is up.
  # ~/.local/bin/ha-host-actions.py is the copy the HA repo deploys (scripts/host/ha-host-actions.py).
  systemd.services.ha-host-actions = {
    description = "Home Assistant host action bridge";
    wantedBy = [ "multi-user.target" ];
    after = [
      "docker.service"
      "ha-compose.service"
      "network-online.target"
    ];
    wants = [ "network-online.target" ];
    # sudo (Kodi start/stop, modules/media.nix), router CLI and the Kodi helpers in ~/.local/bin
    path = haTools ++ [
      "/run/wrappers"
      "/home/ultra/.local"
      pkgs.yt-dlp
    ];
    environment = {
      HA_HOST_ACTIONS_BIND = "172.20.0.1";
      HA_HOST_ACTIONS_PORT = "8787";
      KODI_UNIT = "kodi.service";
      KODI_JSONRPC_URL = "http://127.0.0.1:8080/jsonrpc";
      KODI_USERNAME = "kodi";
      KODI_USERDATA_DIR = "/home/ultra/.kodi/userdata";
      KODI_LIBRARY_DIR = "/home/ultra/KodiLibrary";
      TRANSMISSION_RPC_URL = "http://127.0.0.1:9091/transmission/rpc";
    };
    unitConfig.StartLimitIntervalSec = 0;
    serviceConfig = {
      Type = "simple";
      User = "ultra";
      Group = "ultra";
      # KODI_PASSWORD
      EnvironmentFile = "/home/ultra/.config/ha-host-actions/kodi.env";
      ExecStart = "${pkgs.python3}/bin/python3 /home/ultra/.local/bin/ha-host-actions.py";
      Restart = "always";
      RestartSec = 2;
      RestartSteps = 8;
      RestartMaxDelaySec = "5min";
      LimitNOFILE = 65536;
      MemoryMax = "256M";
    };
  };

  # homeassistant.local on the LAN (HA runs on the host network, so it announces its HomeKit bridges itself).
  systemd.services.ha-mdns = {
    description = "Home Assistant mDNS publisher";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "ha-compose.service"
    ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      User = "ultra";
      Group = "ultra";
      ExecStart = "${mdnsPython}/bin/python3 ${../../../scripts/host/ha-mdns-publish.py}";
      Restart = "on-failure";
      RestartSec = 10;
      MemoryMax = "128M";
    };
  };

  # Soil probe on the board's SAR ADC (iio:device0 channel 3), powered through a GPIO line only while sampling;
  # HA polls http://127.0.0.1:8768/reading. The script is this repository's scripts/soil_moisture/soil_bridge.py.
  systemd.services.ha-soil-moisture = {
    description = "ODROID N2+ soil probe ADC bridge for Home Assistant";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    environment.PYTHONDONTWRITEBYTECODE = "1";
    serviceConfig = {
      Type = "simple";
      ExecStart = "${soilPython}/bin/python3 ${../../../scripts/soil_moisture/soil_bridge.py}";
      Restart = "on-failure";
      RestartSec = 5;
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_UNIX"
      ];
      DevicePolicy = "closed";
      DeviceAllow = [ "/dev/gpiochip0 rw" ];
      MemoryMax = "96M";
    };
  };

  # ultra's Armbian crontab, every 5 minutes: local HA health with fix-all (scripts/ha_fail_safe_cron.sh) and the
  # end-to-end check of HA's external URL (scripts/dataplicity_watchdog.py).
  systemd.services.ha-fail-safe = ultraCheck "Home Assistant fail-safe check" "${checkout}/scripts/ha_fail_safe_cron.sh";
  systemd.services.ha-dataplicity-watchdog = ultraCheck "Home Assistant external URL watchdog" "${checkout}/scripts/dataplicity_watchdog.py";
  systemd.timers = lib.genAttrs [ "ha-fail-safe" "ha-dataplicity-watchdog" ] (_: {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "10min";
      OnUnitActiveSec = "5min";
    };
  });
}
