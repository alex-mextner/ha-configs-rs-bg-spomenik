# Anton's off-site storage: his disk (/mnt/storage, storage.nix, ciphertext only) served over SFTP to his
# Nextcloud, reachable only through his own tailnet. Nothing here reads, writes or formats the disk itself; only
# his rclone process, as his user, touches it.
#
# On Armbian this was his crontab:
#   @reboot ~/.local/state/ts-owner/start.sh         -> tailscaled-anton (below)
#   @reboot + */2 ~/.config/storage-sftp/sftp-start.sh -> anton-storage-sftp + its timer (below)
#   */20 ~/diskbench/collect.sh                        -> anton-diskbench (below)
# His home (/home/anton: keys, tailscale node state, scripts, logs) moves as it is (MIGRATION.md, step 6), so the
# node, its key, ACLs and `tailscale serve` (his port 22 -> 127.0.0.1:2224) stay the same.
{ pkgs, ... }:

let
  home = "/home/anton";
  tsDir = "${home}/.local/state/ts-owner";
  sftpDir = "${home}/.config/storage-sftp";
in
{
  users.groups.anton.gid = 1002;
  users.users.anton = {
    isNormalUser = true;
    uid = 1002;
    group = "anton";
    home = home;
    homeMode = "750";
  };

  # A second tailscaled, his node in his Headscale tailnet: userspace networking, so no TUN device, no netfilter
  # rules and no routes next to ultra's tailscaled (tailscale0, UDP 41641). Socket for his `tailscale` CLI:
  # tailscale --socket=~/.local/state/ts-owner/tailscaled.sock ...
  systemd.services.tailscaled-anton = {
    description = "Anton's tailscaled (userspace networking, his Headscale node)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      User = "anton";
      Group = "anton";
      ExecStart = "${pkgs.tailscale}/bin/tailscaled --tun=userspace-networking --statedir=${tsDir} --socket=${tsDir}/tailscaled.sock --port=41643";
      Restart = "always";
      RestartSec = 5;
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ReadWritePaths = [
        tsDir
        "-${home}/.cache"
      ];
      PrivateTmp = true;
      MemoryMax = "192M";
    };
  };

  # Userspace tailscaled forwards inbound tailnet connections to 127.0.0.1:<port>, and his Headscale ACLs decide
  # which peers get in. Without this filter they would reach every loopback service of the board (Transmission RPC,
  # ESPHome, Glances, bgutil-pot ...). His processes may use loopback only for his SFTP port and DNS.
  # flushRuleset stays off: Docker and ultra's tailscaled keep their own iptables-nft rules.
  networking.nftables = {
    enable = true;
    flushRuleset = false;
    tables.anton-egress = {
      family = "inet";
      content = ''
        chain output {
          type filter hook output priority filter; policy accept;
          # replies of his rclone (source port 2224) to the forwarded connections
          ct state established,related accept
          oifname "lo" meta skuid 1002 tcp dport { 53, 2224 } accept
          oifname "lo" meta skuid 1002 udp dport 53 accept
          oifname "lo" meta skuid 1002 meta l4proto tcp reject with tcp reset
          oifname "lo" meta skuid 1002 reject
        }
      '';
    };
  };

  # rclone SFTP server for /mnt/storage on 127.0.0.1:2224, key login only (his authorized_keys), no shell.
  # It runs only while the real ext4 is mounted there: BindsTo the mount unit, so it stops when the disk drops
  # out (on Armbian rclone kept serving the empty autofs stub and woke the automount every 15 s). Only the timer
  # starts it, every 2 minutes: a no-op while it runs, it brings it back when the disk returns, and a missing
  # disk never holds up boot or a switch.
  systemd.services.anton-storage-sftp = {
    description = "Anton's storage SFTP (rclone serve sftp /mnt/storage on 127.0.0.1:2224)";
    bindsTo = [ "mnt-storage.mount" ];
    after = [ "mnt-storage.mount" ];
    serviceConfig = {
      User = "anton";
      Group = "anton";
      ExecStart = "${pkgs.rclone}/bin/rclone serve sftp /mnt/storage --addr 127.0.0.1:2224 --authorized-keys ${sftpDir}/authorized_keys --key ${sftpDir}/host_ed25519 --log-file ${sftpDir}/sftp.log --log-level NOTICE";
      Restart = "on-failure";
      RestartSec = 10;
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ReadWritePaths = [
        "/mnt/storage"
        sftpDir
      ];
      PrivateTmp = true;
      MemoryMax = "256M";
    };
  };
  systemd.timers.anton-storage-sftp = {
    wantedBy = [ "timers.target" ];
    timerConfig.OnCalendar = "*:0/2";
  };

  # His network benchmark: every 20 minutes ~50 MB down and 50 MB up against speed.cloudflare.com (~7 GB a day).
  systemd.services.anton-diskbench = {
    description = "Anton's network benchmark (~/diskbench/collect.sh)";
    path = with pkgs; [
      bash
      coreutils
      curl
      gawk
      gnugrep
      gnused
      iputils
      python3
    ];
    serviceConfig = {
      Type = "oneshot";
      User = "anton";
      Group = "anton";
      WorkingDirectory = "${home}/diskbench";
      ExecStart = "${pkgs.bash}/bin/bash -c 'exec ${pkgs.bash}/bin/bash ${home}/diskbench/collect.sh >> ${home}/diskbench/cron.log 2>&1'";
      TimeoutStartSec = "15min";
      Nice = 10;
      MemoryMax = "128M";
    };
  };
  systemd.timers.anton-diskbench = {
    wantedBy = [ "timers.target" ];
    timerConfig.OnCalendar = "*:0/20";
  };
}
