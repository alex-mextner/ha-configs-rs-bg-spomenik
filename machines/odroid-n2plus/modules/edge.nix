# Public edge: the Cloudflare Tunnel connector and the mextner.com origin behind it, plus that site's deploy runner.
#
# cloudflared: a remotely managed tunnel; its ingress rules (which hostname goes to which local port) live in the
# Cloudflare dashboard, and this host only holds the token (sops secret cloudflared-token). nixpkgs'
# services.cloudflared configures locally managed tunnels only (credentials file + ingress in Nix), so this is a
# plain unit. The bots' hostnames (finbot*, hypercal) leave this tunnel with the bots (MIGRATION.md, step 0).
#
# caddy-mextner: the mextner.com origin on 127.0.0.1:8081 (Cloudflare terminates TLS). The site repository owns
# its Caddyfile: the deploy job copies site/public to /var/www/mextner.com and installs
# /var/www/mextner.com.caddy/Caddyfile, which caddy picks up with --watch. services.caddy would own the config
# in Nix instead, so this is a plain unit too.
#
# github-runner: alex-mextner/mextner.com's deploy job runs on [self-hosted, odroid] and publishes locally.
{
  config,
  pkgs,
  ...
}:

let
  site = "/var/www/mextner.com";
  siteConfigDir = "/var/www/mextner.com.caddy";
  # One-hour registration token, provisioned by hand right before the runner's first start (MIGRATION.md,
  # step 8). Keep the file afterwards: the runner unit is skipped (no error) while it is missing, even once
  # registered; after the registration only the runner state in /var/lib/github-runner is used.
  runnerTokenFile = "/var/lib/github-runner-tokens/odroid-mextner";
in
{
  systemd.services.cloudflared = {
    description = "Cloudflare Tunnel connector";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "notify";
      DynamicUser = true;
      LoadCredential = "token:${config.sops.secrets.cloudflared-token.path}";
      ExecStart = "${pkgs.cloudflared}/bin/cloudflared --no-autoupdate tunnel run --token-file \${CREDENTIALS_DIRECTORY}/token";
      Restart = "on-failure";
      RestartSec = 5;
      TimeoutStartSec = 15;
      MemoryMax = "192M";
    };
  };

  # Owns the site files; the deploy runner and caddy run as this user.
  users.users.mextner-site = {
    isSystemUser = true;
    group = "mextner-site";
    home = site;
  };
  users.groups.mextner-site = { };
  systemd.tmpfiles.rules = [
    "d ${site} 0755 mextner-site mextner-site -"
    "d ${siteConfigDir} 0755 mextner-site mextner-site -"
    "d /var/lib/github-runner-tokens 0700 root root -"
  ];

  systemd.services.caddy-mextner = {
    description = "Caddy origin for mextner.com (behind Cloudflare Tunnel)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      User = "mextner-site";
      Group = "mextner-site";
      # Caddyfile: { admin off; auto_https off }, http://:8081 bound to 127.0.0.1
      ExecStart = "${pkgs.caddy}/bin/caddy run --config ${siteConfigDir}/Caddyfile --adapter caddyfile --watch";
      Restart = "on-failure";
      Environment = [ "XDG_DATA_HOME=/var/lib/caddy-mextner" ];
      StateDirectory = "caddy-mextner";
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      MemoryMax = "128M";
    };
  };

  services.github-runners.odroid-mextner = {
    enable = true;
    url = "https://github.com/alex-mextner/mextner.com";
    name = "odroid-mextner";
    tokenFile = runnerTokenFile;
    # the Armbian runner of the same name is replaced on registration
    replace = true;
    extraLabels = [ "odroid" ];
    user = "mextner-site";
    group = "mextner-site";
    extraPackages = with pkgs; [
      rsync
      curl
    ];
    serviceOverrides = {
      ReadWritePaths = [
        site
        siteConfigDir
      ];
      MemoryMax = "768M";
    };
  };
  # Skipped, not failed, until the token is provisioned.
  systemd.services.github-runner-odroid-mextner.unitConfig.ConditionPathExists = runnerTokenFile;
}
