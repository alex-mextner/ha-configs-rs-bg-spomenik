# PrintOps (3D printer stock/print API, port 8765) and Spoolman (filament spools, port 7912): the Compose
# project `printops` from the PrintOps checkout in /home/ultra/printops/src. Data: /home/ultra/printops/data
# (SQLite), nightly snapshot to ~/homeassistant/printops-backup (printops-backup timer, ultra-timers.nix).
# Interim: both move to the Mac mini later; until then they stay here.
{ config, ... }:

let
  dir = "/home/ultra/printops/src/services/3d-api";
  limits = "/etc/printops/compose.limits.yaml";
  compose = "${config.virtualisation.docker.package}/bin/docker compose -f ${dir}/compose.yaml -f ${limits}";
in
{
  environment.etc."printops/compose.limits.yaml".text = ''
    services:
      api:
        deploy: {resources: {limits: {memory: 256m}}}
      spoolman:
        deploy: {resources: {limits: {memory: 256m}}}
  '';

  systemd.services.printops = {
    description = "PrintOps API and Spoolman (Docker Compose)";
    wantedBy = [ "multi-user.target" ];
    # wanted, not required: a rebuild that restarts dockerd must not stop the stack (live-restore)
    wants = [
      "docker.service"
      "network-online.target"
    ];
    after = [
      "docker.service"
      "network-online.target"
    ];
    reloadIfChanged = true;
    # with reloadIfChanged a trigger change also becomes a reload, not a restart
    restartTriggers = [ config.environment.etc."printops/compose.limits.yaml".text ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = "ultra";
      Group = "ultra";
      # compose reads services/3d-api/.env from here
      WorkingDirectory = dir;
      ExecStart = "${compose} up -d";
      ExecReload = "${compose} up -d";
      ExecStop = "${compose} stop";
      TimeoutStartSec = "15min"; # builds printops/3d-api:local when the image is missing
      MemoryMax = "256M";
    };
  };
}
