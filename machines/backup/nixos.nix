# NixOS side of machines/backup: nightly restic backup of this host to Google Drive.
#
# One restic repository per host, rclone:<remote>:<folder_prefix>-<host>/restic (policy.toml). What to back up
# comes from hosts/<host>.toml, the same file the Debian hosts use, and every timer runs a lib/homelab-backup verb
# (run, check, drill), exactly as on the Debian hosts: one code path for the dumps, the backup, the retention and
# the status files. Secrets are not in the store: /etc/homelab-backup/restic-password and
# /etc/homelab-backup/rclone.conf are provisioned once by hand, then `homelab-backup init` (README.md, Secrets);
# until then the timers fail and say so in /var/lib/homelab-backup/status-*.json.
#
# A host enables it with
#   imports = [ ../backup/nixos.nix ];  homelab.backup.enable = true;
# and `homelab.backup.host` when its hosts/<name>.toml differs from networking.hostName.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.homelab.backup;
  policy = builtins.fromTOML (builtins.readFile ./policy.toml);
  spec = builtins.fromTOML (builtins.readFile (./hosts + "/${cfg.host}.toml"));
  confDir = "/etc/homelab-backup";
  stateDir = "/var/lib/homelab-backup";
  driveFolder = "${policy.remote}:${policy.folder_prefix}-${spec.host}";

  settings = {
    host = spec.host;
    drive_folder = driveFolder;
    repository = "rclone:${driveFolder}/restic";
    password_file = "${confDir}/restic-password";
    rclone_config = "${confDir}/rclone.conf";
    rclone_program = lib.getExe pkgs.rclone;
    restic_program = lib.getExe pkgs.restic;
    state_dir = stateDir;
    cache_dir = "/var/cache/homelab-backup";
    paths = spec.paths;
    exclude = spec.exclude or [ ];
    dumps = spec.dumps or [ ];
    drill_file = spec.drill_file or "";
    notify = spec.notify or [ ];
    # debian/run implements these two (ubuntu-pc); the assertion below refuses them here
    tasks_offsite = null;
    watch = [ ];
    inherit policy;
  };

  homelab-backup = pkgs.writeShellApplication {
    name = "homelab-backup";
    runtimeInputs =
      with pkgs;
      [
        coreutils
        diffutils
        findutils
        gnused
        jq
        restic
        rclone
        sqlite
        util-linux # flock: one run or dump at a time
      ]
      ++ lib.optional config.virtualisation.docker.enable config.virtualisation.docker.package;
    text = builtins.readFile ./lib/homelab-backup;
  };
  exe = lib.getExe homelab-backup;

  # timedVerb: a oneshot running `homelab-backup <verb>` and its timer, as debian/run writes them
  timedVerb =
    {
      name,
      verb,
      description,
      calendar,
      delay ? "5min",
    }:
    {
      services.${name} = {
        inherit description;
        wants = [ "network-online.target" ];
        after = [ "network-online.target" ];
        # a rebuild must not stop a running backup, prune or drill midway (the next timer run picks up the change)
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${exe} ${verb}";
          Nice = 10;
          IOSchedulingClass = "idle";
          TimeoutStartSec = "12h";
        };
      };
      timers.${name} = {
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnCalendar = calendar;
          RandomizedDelaySec = delay;
          Persistent = true;
        };
      };
    };
in
{
  options.homelab.backup = {
    enable = lib.mkEnableOption "the nightly restic backup to Google Drive (machines/backup)";
    host = lib.mkOption {
      type = lib.types.str;
      default = config.networking.hostName;
      defaultText = lib.literalExpression "config.networking.hostName";
      description = "Name of the machines/backup/hosts/<host>.toml that lists what this machine backs up.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = (spec.scope or "system") == "system";
        message = "machines/backup/hosts/${cfg.host}.toml: NixOS hosts back up as root (scope = \"system\").";
      }
      {
        assertion = (spec.watch or [ ]) == [ ] && !(spec ? tasks_offsite);
        message = "machines/backup/hosts/${cfg.host}.toml: `watch` and [tasks_offsite] are implemented by debian/run only.";
      }
      {
        assertion = builtins.isList (spec.paths or null) && spec.paths != [ ];
        message = "machines/backup/hosts/${cfg.host}.toml: paths must be a non-empty list.";
      }
      {
        assertion = lib.versionAtLeast pkgs.restic.version "0.17";
        message = "machines/backup needs restic 0.17 or newer (exit code 10), nixpkgs has ${pkgs.restic.version}.";
      }
    ];

    environment.etc."homelab-backup/config.json".text = builtins.toJSON settings;
    # rclone too: the one-time `rclone config` of the Secrets steps runs on the host
    environment.systemPackages = [
      homelab-backup
      pkgs.rclone
    ];
    systemd = lib.mkMerge [
      {
        tmpfiles.rules = [
          "d ${confDir} 0700 root root -"
          "d ${stateDir} 0700 root root -"
        ];
      }
      (timedVerb {
        name = "homelab-backup";
        verb = "run";
        description = "homelab-backup: database dumps + restic backup to Google Drive";
        calendar = policy.schedule.backup;
        delay = policy.schedule.backup_randomized_delay;
      })
      (timedVerb {
        name = "homelab-backup-check";
        verb = "check";
        description = "homelab-backup: restic check --read-data-subset=${policy.check.read_data_subset}";
        calendar = policy.schedule.check;
      })
      (timedVerb {
        name = "homelab-backup-drill";
        verb = "drill";
        description = "homelab-backup: monthly restore drill";
        calendar = policy.schedule.drill;
      })
    ];
  };
}
