# Host config for rpi4: Raspberry Pi 4 Model B (2 GB) driving the wall display. Its only job is
# the kiosk: cage + Chromium on Home Assistant, logged in by kiosk-ha-login. Ported from
# https://github.com/alex-mextner/rpi-nixos-display (the state that ran on the Pi); the k3s +
# ArgoCD cluster it also carried is gone (modules/k3s-teardown.nix clears what is left of it).
{ pkgs, ... }:

let
  homeAssistant = "http://odroidn2:8123";
in
{
  imports = [
    ./modules/options.nix
    ./modules/hardware.nix
    ./modules/secrets.nix
    ./modules/wifi.nix
    ./modules/ssh.nix
    ./modules/kiosk.nix
    ./modules/kiosk-ha-login.nix
    ./modules/autoupgrade.nix
    ./modules/k3s-teardown.nix
  ];

  rpi4.kiosk = {
    # The first page is the active one, the others open in background tabs.
    urls = [ homeAssistant ];
    # null turns the auto-login off; otherwise it needs the secrets ha-username and ha-password.
    homeAssistant.url = homeAssistant;
  };

  # The image on the SD card was built from nixos-26.05; never change after the first install.
  system.stateVersion = "26.05";
  networking.hostName = "rpi4";

  # Neither the ISP's DNS nor mDNS resolves odroidn2 reliably (the odroid also announces its
  # docker address 172.18.0.1), so pin it. The IP is fixed by a DHCP reservation on the router.
  networking.hosts."192.168.0.39" = [ "odroidn2" ];

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    auto-optimise-store = true;
  };

  # Old generations must not fill up the SD card.
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 14d";
  };

  environment.systemPackages = with pkgs; [
    git
    vim
    htop
    iw
    sops
    age
    ssh-to-age
  ];
}
