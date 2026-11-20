# Host config for odroidn2: the headless home server on the ODROID-N2+ that replaces the Armbian install
# (Home Assistant and its sidecars, LAN DHCP/DNS, Kodi on the projector, Anton's storage). What runs here and
# why: README.md; the move from Armbian: MIGRATION.md.
{
  modulesPath,
  pkgs,
  ...
}:

let
  # Same key that is authorized for ultra@odroidn2 today ("ultra@home", SHA256:WdzrznQK...).
  ultraKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdEyhpamHIpibpBBnExkEe/sTf4ga0frBRWEQhBLQtD ultra@home";
in
{
  imports = [
    "${modulesPath}/profiles/minimal.nix"
    ./modules/nic-alive.nix
    ./modules/secrets.nix
    ./modules/storage.nix
    ./modules/memory.nix
    ./modules/home-assistant.nix
    ./modules/home-lan.nix
    ./modules/printops.nix
    ./modules/media.nix
    ./modules/edge.nix
    ./modules/ultra-timers.nix
    ./modules/anton.nix
    ../dotfiles/nixos.nix
  ];

  # The Armbian name: the tailnet name odroidn2.<tailnet>.ts.net (go2rtc WebRTC candidates, tailscale serve)
  # and the backup host file stay the same. Armbian and NixOS never run at the same time.
  networking.hostName = "odroidn2";

  # This board is the LAN's DHCP server (home-lan-dhcp, modules/home-lan.nix), so its own address is static.
  networking.useNetworkd = true;
  networking.useDHCP = false;
  systemd.network.networks."10-lan" = {
    matchConfig.Name = "end0 eth0";
    networkConfig = {
      Address = "192.168.0.39/24";
      Gateway = "192.168.0.1";
      DNS = [
        "89.216.1.30"
        "89.216.1.40"
        "89.216.1.50"
      ];
    };
    linkConfig.RequiredForOnline = "routable";
  };

  # Armbian had no packet filter, and Home Assistant, Music Assistant (AirPlay/PTP 319/320, stream ports), HomeKit,
  # Cast and SSDP use host networking with ports that change. The board sits on the home LAN behind the router; the
  # Docker-published ports bypass the NixOS filter anyway. Same exposure as before the move.
  networking.firewall.enable = false;

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # uid/gid 1000 as on Armbian: /home/ultra, the HA config and the container bind mounts are owned by 1000:1000.
  users.groups.ultra.gid = 1000;
  users.users.ultra = {
    isNormalUser = true;
    uid = 1000;
    group = "ultra";
    extraGroups = [
      "wheel"
      "docker"
      "video" # /dev/lirc0 and /dev/dri are root:video
      "render"
      "audio"
      "input"
    ];
    openssh.authorizedKeys.keys = [ ultraKey ];
  };
  # Key-only account without a password; wheel must be able to sudo.
  security.sudo.wheelNeedsPassword = false;

  services.tailscale.enable = true;

  zramSwap = {
    enable = true;
    algorithm = "zstd";
    memoryPercent = 50;
  };

  environment.systemPackages = with pkgs; [
    libgpiod
    usbutils
    htop
    sops
    rsync
  ];

  # ~/.files for ultra, zsh as its login shell (../dotfiles/nixos.nix)
  homelab.dotfiles.enable = true;

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    trusted-users = [ "@wheel" ];
    # 3.7 GB of RAM shared with Home Assistant: one build at a time. Evaluate and build on a bigger machine
    # (`nixos-rebuild switch --flake ./machines#odroid-n2plus --target-host ultra@odroidn2 --sudo`), README.md.
    max-jobs = 1;
    cores = 2;
  };

  time.timeZone = "Europe/Belgrade";

  system.stateVersion = "26.05";
}
