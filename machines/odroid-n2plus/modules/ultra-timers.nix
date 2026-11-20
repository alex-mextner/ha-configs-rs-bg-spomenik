# ultra's periodic jobs: on Armbian these were systemd user units in ~/.config/systemd/user (linger on) with
# Debian paths (/usr/bin/python3, /usr/bin/docker, /usr/bin/ssh). Here they are system units running as ultra
# with the same schedules; MIGRATION.md step 7 removes the old user-unit links from the copied home.
#   printops-backup         04:15  PrintOps + Spoolman DB snapshot into ~/homeassistant/printops-backup
#   router-discover          5 min  router CLI (~/dev/router-cli): device discovery for HA
#   router-inventory-update  1 h    router CLI: inventory refresh
#   router-scan              6 h    router CLI: full scan of online devices
#   tasks-backup-pull        1 h    pull the age-encrypted Tasks backups from bread (~/.local/lib/tasks-backup)
#   tasks-backup-drill       Sun 04:30  restore drill of the newest pulled set
# The drill ran through a forced-command key over ssh to 127.0.0.1 (~/.ssh/authorized_keys, Debian paths in the
# command); it now runs the same command directly, as the same user.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  docker = config.virtualisation.docker.package;
  home = "/home/ultra";

  job =
    {
      description,
      command,
      path ? [ ],
      environment ? { },
      timeout ? "30min",
      memory ? "256M",
      timer,
    }:
    {
      service = {
        inherit description path environment;
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        serviceConfig = {
          Type = "oneshot";
          User = "ultra";
          Group = "ultra";
          WorkingDirectory = home;
          ExecStart = command;
          TimeoutStartSec = timeout;
          Nice = 10;
          IOSchedulingClass = "idle";
          MemoryMax = memory;
        };
      };
      timer = {
        wantedBy = [ "timers.target" ];
        timerConfig = timer // {
          Persistent = timer ? OnCalendar;
        };
      };
    };

  routerPath = with pkgs; [
    python3
    iproute2
    iputils
    coreutils
  ];
  router = "${home}/.local/bin/router";
  routerEnv.ROUTER_CLI_HA_CONFIG = "${home}/homeassistant";
  tasksEnv.PYTHONPATH = "${home}/.local/lib/tasks-backup";
  tasksPath = with pkgs; [
    python3
    age
    zstd
    openssh
    postgresql_17
    docker
    coreutils
  ];

  jobs = {
    printops-backup = job {
      description = "PrintOps + Spoolman database snapshot";
      command = "${docker}/bin/docker exec printops-api 3d printops backup";
      timer.OnCalendar = "*-*-* 04:15:00";
    };
    router-discover = job {
      description = "router CLI: device discovery";
      command = "${router} discover --json";
      path = routerPath;
      environment = routerEnv;
      timer = {
        OnBootSec = "5min";
        OnUnitActiveSec = "5min";
      };
    };
    router-inventory-update = job {
      description = "router CLI: inventory update";
      command = "${router} inventory update --wait 40";
      path = routerPath;
      timer = {
        OnBootSec = "15min";
        OnUnitActiveSec = "1h";
      };
    };
    router-scan = job {
      description = "router CLI: scan of all online devices";
      command = "${router} scan --all-online --json";
      path = routerPath;
      timer = {
        OnBootSec = "30min";
        OnUnitActiveSec = "6h";
      };
    };
    tasks-backup-pull = job {
      description = "Pull the encrypted Tasks backups from bread";
      command = "${pkgs.python3}/bin/python3 -m tasks_backup.pull";
      path = tasksPath;
      environment = tasksEnv;
      timer.OnCalendar = "hourly";
    };
    tasks-backup-drill = job {
      description = "Restore drill of the pulled Tasks backups";
      command = "${pkgs.python3}/bin/python3 -m tasks_backup.drill";
      path = tasksPath;
      environment = tasksEnv;
      timeout = "2h";
      memory = "768M";
      timer.OnCalendar = "Sun *-*-* 04:30:00";
    };
  };
in
{
  systemd.services = lib.mapAttrs (_: j: j.service) jobs;
  systemd.timers = lib.mapAttrs (_: j: j.timer) jobs;
}
