# Home Assistant auto-login for the kiosk.
# kiosk-ha-login walks HA's login_flow with the account from the sops secrets ha-username and
# ha-password and puts the tokens into the localStorage of the Chromium tab over CDP
# (127.0.0.1:9222). The secrets reach the process only as systemd credentials (LoadCredential):
# not in argv, not in the environment, not in /nix/store and not in the journal.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.rpi4.kiosk;
  haUrl = cfg.homeAssistant.url;
  python = pkgs.python3.withPackages (ps: [ ps.websockets ]);
in
{
  config = lib.mkIf (haUrl != null) {
    assertions = [
      {
        assertion = builtins.elem haUrl cfg.urls;
        message = "rpi4.kiosk.homeAssistant.url (${haUrl}) must be one of rpi4.kiosk.urls";
      }
    ];

    # Values in ../secrets/secrets.sops.yaml (keys ha-username and ha-password), readable by root/PID 1 only.
    sops.secrets = {
      ha-username.restartUnits = [ "kiosk-ha-login.service" ];
      ha-password.restartUnits = [ "kiosk-ha-login.service" ];
    };

    systemd.services.kiosk-ha-login = {
      description = "Home Assistant auto-login for the kiosk (tokens into Chromium localStorage over CDP)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [
        "network-online.target"
        "cage-tty1.service"
      ];
      environment = {
        HA_URL = haUrl;
        CDP_URL = "http://127.0.0.1:9222";
      };
      serviceConfig = {
        ExecStart = "${python}/bin/python3 -u ${./kiosk-ha-login.py}";
        LoadCredential = [
          "ha-username:${config.sops.secrets.ha-username.path}"
          "ha-password:${config.sops.secrets.ha-password.path}"
        ];
        Restart = "always";
        RestartSec = 10;
        DynamicUser = true;
        # Network only: HA over the LAN and CDP on localhost; AF_UNIX for nscd, through which
        # NixOS resolves names with NSS modules (mDNS .local, myhostname).
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectControlGroups = true;
        MemoryMax = "128M";
      };
    };
  };
}
