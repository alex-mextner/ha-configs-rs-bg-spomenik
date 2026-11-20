# Auto-upgrade: once a day the Pi fetches main of this repo from GitHub and switches to
# machines/flake.nix#rpi4. No reboot: kernel and bootloader changes apply at the next boot.
{
  system.autoUpgrade = {
    enable = true;
    flake = "github:alex-mextner/ha-configs-rs-bg-spomenik?dir=machines#rpi4";
    operation = "switch";
    allowReboot = false;
    dates = "04:00";
    randomizedDelaySec = "30min";
    persistent = true; # a Pi that was off at 04:00 catches up after boot (with up to 30 min delay)
  };

  # Evaluating the flake takes 0.4-1 GB of RAM, a lot on a 2 GB Pi next to Chromium: when memory
  # runs out the kernel must kill the upgrade, not the kiosk. zram (hardware.nix) covers the rest.
  systemd.services.nixos-upgrade.serviceConfig = {
    OOMScoreAdjust = 1000;
    Nice = 19;
  };
}
