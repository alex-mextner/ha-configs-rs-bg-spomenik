# NixOS homelab machines (see README.md).
#
# Outputs:
#   nixosConfigurations.macmini                      - Mac mini Mid 2010
#   nixosConfigurations.odroid-n2plus                - ODROID-N2+ (hostname odroidn2)
#   packages.aarch64-linux.odroid-n2plus-sdImage     - ODROID-N2+ SD/eMMC image (.img.zst), U-Boot at sector 1
#   packages.aarch64-linux.odroid-n2plus-uboot       - mainline U-Boot (bl33) for odroid-n2_defconfig
#   packages.x86_64-linux.odroid-n2plus-ubootFip     - ODROID-N2+ signed boot blob (u-boot.bin.sd.bin)
#   packages.x86_64-linux.odroid-n2plus-sdImage      - same image, built from an x86_64 host with aarch64 emulation
#   nixosConfigurations.rpi4                         - Raspberry Pi 4 wall-display kiosk
#   packages.aarch64-linux.rpi4-sdImage              - Raspberry Pi 4 SD image (rpi4-kiosk.img.zst)
#
# The ODROID signed boot blob is produced by Amlogic's x86_64-only `aml_encrypt_g12b`, so building the SD image needs
# an x86_64-linux builder in addition to aarch64-linux (native or binfmt). `nixos-rebuild` of the running system does
# not depend on it.
{
  description = "NixOS homelab machines (see README.md)";

  inputs = {
    # nixos-26.05, locked to the revision of the installer ISO
    # (nixos-minimal-26.05.14630.f68407657ad2), so installs fetch binaries
    # already cached for that channel. Bump with `nix flake update`.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # Hardkernel/Amlogic BL2/BL30/BL31 blobs and the signing tool for the ODROID-N2+, as packaged by LibreELEC
    # (the source recommended by U-Boot's doc/board/amlogic/odroid-n2.rst).
    amlogic-boot-fip = {
      url = "github:LibreELEC/amlogic-boot-fip/b54e3bbbcf5613d451230d359d264844eeda7166";
      flake = false;
    };
    # Secrets of rpi4 (Wi-Fi, Home Assistant login) and odroid-n2plus (Cloudflare Tunnel token, Transmission RPC
    # whitelist), decrypted on each host with its SSH host key.
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      amlogic-boot-fip,
      sops-nix,
      ...
    }:
    let
      pkgsArm = nixpkgs.legacyPackages.aarch64-linux;
      pkgsX86 = nixpkgs.legacyPackages.x86_64-linux;

      odroidUboot = pkgsArm.callPackage ./odroid-n2plus/pkgs/uboot-odroid-n2.nix { };
      odroidUbootFip = pkgsX86.callPackage ./odroid-n2plus/pkgs/uboot-odroid-n2plus-fip.nix {
        uboot = odroidUboot;
        fipSource = amlogic-boot-fip;
      };

      odroid = nixpkgs.lib.nixosSystem {
        specialArgs.ubootFip = odroidUbootFip;
        modules = [
          ./odroid-n2plus/hardware/odroid-n2plus.nix
          sops-nix.nixosModules.sops
          ./odroid-n2plus/configuration.nix
          ./backup/nixos.nix
          {
            homelab.backup = {
              enable = true;
              # backup/hosts/odroidn2-nixos.toml: the Armbian install's repository (host "odroidn2"), without the bots
              host = "odroidn2-nixos";
            };
          }
        ];
      };

      rpi4 = nixpkgs.lib.nixosSystem {
        modules = [
          sops-nix.nixosModules.sops
          ./rpi4/configuration.nix
        ];
      };
    in
    {
      nixosConfigurations.macmini = nixpkgs.lib.nixosSystem {
        modules = [
          ./macmini/configuration.nix
          ./backup/nixos.nix
          { homelab.backup.enable = true; }
        ];
      };

      nixosConfigurations.odroid-n2plus = odroid;

      nixosConfigurations.rpi4 = rpi4;

      packages.aarch64-linux = {
        odroid-n2plus-uboot = odroidUboot;
        odroid-n2plus-sdImage = odroid.config.system.build.sdImage;
        rpi4-sdImage = rpi4.config.system.build.sdImage;
      };

      packages.x86_64-linux = {
        odroid-n2plus-ubootFip = odroidUbootFip;
        odroid-n2plus-sdImage = odroid.config.system.build.sdImage;
      };
    };
}
