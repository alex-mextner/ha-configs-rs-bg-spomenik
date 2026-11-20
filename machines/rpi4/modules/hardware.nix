# Pure hardware description of the Raspberry Pi 4. The impure /etc/nixos/hardware-configuration.nix
# cannot be used in a flake, so this takes the nixpkgs module the SD image was built from:
# sd-image-aarch64. It sets the mainline kernel, U-Boot + extlinux, fileSystems by the labels
# NIXOS_SD (/) and FIRMWARE (/boot/firmware, noauto), and grows the partition on first boot.
{ lib, modulesPath, ... }:
{
  imports = [ "${modulesPath}/installer/sd-card/sd-image-aarch64.nix" ];

  nixpkgs.hostPlatform = "aarch64-linux";

  # Wi-Fi (brcmfmac) firmware and the like, as in the original image.
  hardware.enableRedistributableFirmware = true;

  # VideoCore VI graphics (Mesa v3d/vc4) for Chromium under Wayland.
  hardware.graphics.enable = true;

  # Stable image name for scripts/flash-sd.sh: rpi4-kiosk.img.zst (the default name carries the
  # version and date, so the artifact path changes every build).
  image.baseName = "rpi4-kiosk";

  # Compressed swap in RAM, sized to the whole 2 GB (the default is 50%): Chromium plus the nightly
  # nixos-upgrade (flake evaluation) do not fit in RAM alone. Writes nothing to the SD card. The
  # size only changes at boot: swapoff on the live system would pull every page back into RAM.
  zramSwap = {
    enable = true;
    memoryPercent = 100;
  };

  # The installer profile behind sd-image-aarch64 enables ZFS (module, zfs-zed); the root is ext4.
  boot.supportedFilesystems.zfs = lib.mkForce false;

  boot.kernelParams = [
    # HDMI does not blank after 10 minutes and keeps a signal even if the screen is turned on later.
    "consoleblank=0"
    "video=HDMI-A-1:1920x1080M@60D"
  ];
}
