# Mainline U-Boot (bl33) for ODROID-N2 and N2+. Both boards share odroid-n2_defconfig;
# U-Boot tells them apart by SARADC at runtime (doc/board/amlogic/odroid-n2.rst).
# This u-boot.bin is not bootable on its own: pkgs/uboot-odroid-n2plus-fip.nix wraps it with
# the Amlogic BL2/BL30/BL31 firmware into u-boot.bin.sd.bin.
{ buildUBoot }:

buildUBoot {
  defconfig = "odroid-n2_defconfig";
  extraMeta.platforms = [ "aarch64-linux" ];
  filesToInstall = [ "u-boot.bin" ];
}
