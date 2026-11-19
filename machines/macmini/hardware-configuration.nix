# Hardware configuration for the Mac mini Mid 2010 (Macmini4,1).
#
# Hand-written so the flake is self-contained: filesystems are mounted by the
# labels that install-steps.md creates (BOOT, nixos), so no per-install UUIDs.
# The initrd module list covers the MCP89 SATA controller (ahci) and the
# USB/FireWire/SD controllers. During install, compare with the output of
# `nixos-generate-config --show-hardware-config` and add any module it lists
# that is missing here.
{ lib, modulesPath, ... }:

{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];

  boot.initrd.availableKernelModules = [
    "ahci"
    "ohci_pci"
    "ehci_pci"
    "firewire_ohci"
    "usbhid"
    "usb_storage"
    "sd_mod"
    "sdhci_pci"
  ];
  boot.kernelModules = [ "kvm-intel" ];

  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  fileSystems."/boot" = {
    device = "/dev/disk/by-label/BOOT";
    fsType = "vfat";
    options = [
      "fmask=0077"
      "dmask=0077"
    ];
  };

  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
