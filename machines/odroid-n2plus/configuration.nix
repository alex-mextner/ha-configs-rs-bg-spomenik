# Host config for odroidn2-nixos: minimal headless server (no desktop) that will take over from
# the Armbian install on the ODROID-N2+ (Home Assistant + home LAN DHCP/DNS in Docker).
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
    ../dotfiles/nixos.nix
  ];

  networking.hostName = "odroidn2-nixos";

  # This board is itself the LAN's DHCP server (home-lan-dhcp container on Armbian), so while
  # this image runs nobody may answer DHCP. Ask anyway, and keep the address Armbian uses as a
  # static one so the box is always reachable at 192.168.0.39.
  networking.useNetworkd = true;
  networking.useDHCP = false;
  systemd.network.networks."10-lan" = {
    matchConfig.Name = "end0 eth0";
    networkConfig = {
      DHCP = "ipv4";
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

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  users.users.ultra = {
    isNormalUser = true;
    extraGroups = [
      "wheel"
      "docker"
      "video" # /dev/lirc0 and /dev/dri are root:video
    ];
    openssh.authorizedKeys.keys = [ ultraKey ];
  };
  # Key-only account without a password; wheel must be able to sudo.
  security.sudo.wheelNeedsPassword = false;

  virtualisation.docker.enable = true;

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
  ];

  # ~/.files for ultra, zsh as its login shell (../dotfiles/nixos.nix)
  homelab.dotfiles.enable = true;

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    trusted-users = [ "@wheel" ];
  };

  time.timeZone = "Europe/Belgrade";

  system.stateVersion = "26.05";
}
