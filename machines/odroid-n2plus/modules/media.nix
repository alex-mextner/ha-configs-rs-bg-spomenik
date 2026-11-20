# Media: Kodi on the HDMI projector (on demand), Transmission, and Glances (host metrics for HA).
#
# Kodi is never started at boot. HA's host action bridge starts and stops kodi.service with `sudo -n systemctl`
# (ultra is in wheel without a password) when the projector is switched on. With the projector on HDMI, Kodi
# drives it directly through GBM/KMS (kodi-gbm, panfrost); with nothing connected after 90 s it runs inside Xvfb
# with the X11 build, so the JSON-RPC API, library scans and HA's media_player entity still work. State: ~/.kodi
# (web server on 8080 for JSON-RPC).
#
# Transmission is the nixpkgs module, run as ultra with the same config dir as on Armbian
# (~/.config/transmission-daemon; downloads and Kodi library symlinks share one owner). Downloads land on the
# media disk (~/Downloads is a bind of /mnt/wd500/torrents, storage.nix). Interim: moves to the Mac mini later.
{
  config,
  pkgs,
  ...
}:

let
  kodiX11 = pkgs.kodi;
  kodiGbm = pkgs.kodi-gbm;

  kodiSession = pkgs.writeShellScript "kodi-session" ''
    set -eu
    hdmi_connected() {
      for status in /sys/class/drm/*-HDMI-A-*/status; do
        [ -r "$status" ] && [ "$(cat "$status")" = connected ] && return 0
      done
      return 1
    }
    # The power-pair controller may take 5 s to see Kodi start and the projector needs about 18 s to boot: wait
    # for its full startup deadline before choosing headless.
    waited=0
    while [ "$waited" -lt 90 ] && ! hdmi_connected; do
      sleep 1
      waited=$((waited + 1))
    done
    if hdmi_connected; then
      exec ${kodiGbm}/bin/kodi --standalone --windowing=gbm
    fi
    ${pkgs.xvfb}/bin/Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
    export DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1
    sleep 2
    exec ${kodiX11}/bin/kodi --standalone --windowing=x11
  '';

  # Stop kodi.bin first and wait for it to save its state (18-30 s, #61); only then does systemd signal the
  # launcher and kill what is left (Xvfb). Bounded by TimeoutStopSec.
  kodiStop = pkgs.writeShellScript "kodi-stop" ''
    ${pkgs.procps}/bin/pkill -TERM -u ultra -x kodi.bin || exit 0
    while ${pkgs.procps}/bin/pgrep -u ultra -x kodi.bin >/dev/null; do
      sleep 1
    done
  '';
in
{
  hardware.graphics.enable = true;
  # HDMI audio routing of the G12B sound card (mixer controls), carried over from Armbian's
  # /var/lib/alsa/asound.state by MIGRATION.md and restored at boot.
  hardware.alsa.enablePersistence = true;

  systemd.services.kodi = {
    description = "Kodi media center (on demand, started by ha-host-actions via sudo)";
    after = [ "sound.target" ];
    path = [ pkgs.coreutils ];
    environment = {
      HOME = "/home/ultra";
      XDG_RUNTIME_DIR = "/run/kodi";
    };
    serviceConfig = {
      Type = "simple";
      User = "ultra";
      Group = "ultra";
      SupplementaryGroups = [
        "video"
        "render"
        "audio"
        "input"
      ];
      RuntimeDirectory = "kodi";
      RuntimeDirectoryMode = "0700";
      # Measured ODROID-N2 start failure: compact memory for the CMA pool before Kodi allocates its buffers
      # (this repository's scripts/host/ha_kodi_prestart.py, as root; failure is not fatal).
      ExecStartPre = "-+${pkgs.coreutils}/bin/timeout --kill-after=2s 55s ${pkgs.python3}/bin/python3 -I ${../../../scripts/host/ha_kodi_prestart.py}";
      ExecStart = kodiSession;
      ExecStop = kodiStop;
      Restart = "no";
      # Kodi's launcher script (the main process) traps SIGTERM, forwards it to kodi.bin and exits 143 at once; under
      # KillMode=mixed systemd would then SIGKILL kodi.bin, so ExecStop above lets kodi.bin finish first.
      SuccessExitStatus = 143;
      KillMode = "mixed";
      TimeoutStopSec = "1min";
      # HA, Music Assistant and the voice stack share 3.7 GB: a runaway Kodi must die alone.
      MemoryHigh = "900M";
      MemoryMax = "1200M";
      OOMScoreAdjust = 500;
      UMask = "0077";
      # BFQ weight 500 (default 100): responsive playback while HA keeps the higher weight
      IOWeight = 4500;
    };
  };

  services.transmission = {
    enable = true;
    package = pkgs.transmission_4;
    user = "ultra";
    group = "ultra";
    home = "/home/ultra";
    # ~/Downloads already exists (bind mount); keep Armbian's modes.
    downloadDirPermissions = null;
    # Non-default settings of the Armbian settings.json; the module merges them into
    # ~/.config/transmission-daemon/settings.json at every start (no RPC credentials: auth is off, whitelist on).
    settings = {
      download-dir = "/home/ultra/Downloads";
      incomplete-dir = "/home/ultra/Downloads";
      incomplete-dir-enabled = false;
      watch-dir-enabled = false;
      message-level = 4;
      peer-port = 51413;
      peer-port-random-on-start = false;
      port-forwarding-enabled = true;
      cache-size-mb = 4;
      rpc-enabled = true;
      rpc-bind-address = "0.0.0.0";
      rpc-port = 9091;
      rpc-authentication-required = false;
      rpc-host-whitelist-enabled = false;
      rpc-whitelist-enabled = true;
      # loopback, HA's docker network and the LAN address; sops `transmission-settings` replaces it with the same
      # list plus this host's tailnet address (kept out of this public repository)
      rpc-whitelist = "127.0.0.1,::1,172.20.*.*,192.168.0.39";
      umask = "022";
    };
    credentialsFile = config.sops.secrets.transmission-settings.path;
  };
  systemd.services.transmission = {
    unitConfig.RequiresMountsFor = [ "/home/ultra/Downloads" ];
    serviceConfig.MemoryMax = "256M";
  };
  systemd.services.transmission-setup.serviceConfig.MemoryMax = "32M";

  # HA (packages/glances_monitor.yaml) polls the REST API /api/4 on loopback; the web UI is off.
  services.glances = {
    enable = true;
    # Not in the binary cache for aarch64-linux, and its REST tests need a network the build sandbox does not have.
    package = pkgs.glances.overridePythonAttrs { doCheck = false; };
    port = 61208;
    extraArgs = [
      "--webserver"
      "--bind"
      "127.0.0.1"
      "--disable-webui"
      "--time"
      "30"
    ];
  };
  systemd.services.glances.serviceConfig.MemoryMax = "160M";
}
