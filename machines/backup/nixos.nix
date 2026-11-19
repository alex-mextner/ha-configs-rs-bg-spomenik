# NixOS side of machines/backup: nightly restic backup of this host to Google Drive.
#
# One restic repository per host, rclone:<remote>:<root>/restic/<host> (policy.toml), filled by nixpkgs'
# services.restic.backups.homelab. What to back up comes from hosts/<host>.toml, the same file the Debian hosts
# use; the dump, path-list, check and drill logic is lib/homelab-backup, shared with debian/. Secrets are not in
# the store: /etc/homelab-backup/restic-password and /etc/homelab-backup/rclone.conf are provisioned once by hand
# (README.md, Secrets); until they exist the timers fail and say so in /var/lib/homelab-backup/status-*.json.
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
  rcloneExe = lib.getExe pkgs.rclone;

  settings = {
    host = spec.host;
    repository = "rclone:${policy.remote}:${policy.root}/restic/${spec.host}";
    password_file = "${confDir}/restic-password";
    rclone_config = "${confDir}/rclone.conf";
    rclone_program = rcloneExe;
    restic_program = lib.getExe pkgs.restic;
    state_dir = stateDir;
    cache_dir = "/var/cache/homelab-backup";
    paths = spec.paths;
    exclude = spec.exclude or [ ];
    dumps = spec.dumps or [ ];
    drill_file = spec.drill_file or "";
    notify = spec.notify or [ ];
    tasks_offsite = null;
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
        util-linux # setpriv: SQLite dumps run as the database owner
      ]
      ++ lib.optional config.virtualisation.docker.enable config.virtualisation.docker.package;
    text = builtins.readFile ./lib/homelab-backup;
  };
  exe = lib.getExe homelab-backup;

  # weekly check and monthly drill: lib/homelab-backup, scheduled like on the Debian hosts
  scheduled = verb: description: calendar: {
    services."homelab-backup-${verb}" = {
      inherit description;
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${exe} ${verb}";
        Nice = 10;
        IOSchedulingClass = "idle";
      };
    };
    timers."homelab-backup-${verb}" = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = calendar;
        RandomizedDelaySec = "5min";
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

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = [
          {
            assertion = (spec.scope or "system") == "system";
            message = "machines/backup/hosts/${cfg.host}.toml: NixOS hosts back up as root (scope = \"system\").";
          }
        ];

        environment.etc."homelab-backup/config.json".text = builtins.toJSON settings;
        # rclone too: the one-time `rclone config` of the Secrets steps runs on the host
        environment.systemPackages = [
          homelab-backup
          pkgs.rclone
        ];
        systemd.tmpfiles.rules = [
          "d ${confDir} 0700 root root -"
          "d ${stateDir} 0700 root root -"
        ];

        services.restic.backups.homelab = {
          inherit (settings) repository;
          passwordFile = settings.password_file;
          rcloneConfigFile = settings.rclone_config;
          extraOptions = [ "rclone.program=${rcloneExe}" ];
          initialize = true;
          # the dump staging dir + the host's paths that exist, and the excludes (live SQLite files included)
          dynamicFilesFrom = "${exe} paths";
          # a failed dump must not stop the backup of everything else: it is recorded in status-dump.json
          # (and turns status-run.json into a failure below); the `||` stays outside homelab-backup's own shell
          backupPrepareCommand = ''
            ${exe} dump || true
            ${exe} excludes > /run/restic-backups-homelab/excludes
          '';
          extraBackupArgs = [
            # wait for a running weekly check / monthly drill instead of failing on the repository lock
            "--retry-lock 2h"
            "--host ${spec.host}"
            "--tag nightly"
            "--exclude-caches"
            "--exclude-file=/run/restic-backups-homelab/excludes"
          ];
          pruneOpts = [
            "--retry-lock 2h"
            "--host ${spec.host}"
            "--group-by host"
            "--keep-daily ${toString policy.retention.keep_daily}"
            "--keep-weekly ${toString policy.retention.keep_weekly}"
            "--keep-monthly ${toString policy.retention.keep_monthly}"
          ];
          # the full check runs weekly (homelab-backup-check), not after every backup
          runCheck = false;
          timerConfig = {
            OnCalendar = policy.schedule.backup;
            RandomizedDelaySec = policy.schedule.backup_randomized_delay;
            Persistent = true;
          };
          # ExecStopPost: status-run.json for monitoring, whatever the result; failed dumps fail the run too
          backupCleanupCommand = ''
            if [ "$SERVICE_RESULT" != success ]; then
              ${exe} record run fail "restic-backups-homelab: $SERVICE_RESULT ($EXIT_CODE $EXIT_STATUS), see journalctl -u restic-backups-homelab"
            elif ! ${lib.getExe pkgs.jq} -e .ok ${stateDir}/status-dump.json > /dev/null; then
              ${exe} record run fail "$(${lib.getExe pkgs.jq} -r .message ${stateDir}/status-dump.json) (the snapshot holds the previous dumps of those)"
            else
              ${exe} record run ok
            fi
          '';
        };
      }
      {
        systemd = lib.mkMerge [
          (scheduled "check" "homelab-backup: restic check --read-data-subset=${policy.check.read_data_subset}" policy.schedule.check)
          (scheduled "drill" "homelab-backup: monthly restore drill" policy.schedule.drill)
        ];
      }
    ]
  );
}
