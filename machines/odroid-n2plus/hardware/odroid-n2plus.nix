# Board support for the Hardkernel ODROID-N2+ on the stock NixOS aarch64 kernel.
#
# Everything the board needs is already in the generic nixpkgs aarch64 kernel config
# (checked against linux-config-6.18.63 from nixos-26.05): ARCH_MESON, MMC_MESON_GX=y,
# MESON_SARADC=y, IR_MESON=m, DWMAC_MESON=m + REALTEK_PHY=y, DRM_MESON(+DW_HDMI)=m,
# DRM_PANFROST=m, USB_DWC3_MESON_G12A=y, MESON_GXBB_WATCHDOG=m. No kernel rebuild, no overlays.
#
# Boot chain: Amlogic BootROM -> BL2/BL30/BL31 (vendor blobs) -> mainline U-Boot ->
# /boot/extlinux/extlinux.conf on the ext4 root partition (marked bootable).
{
  config,
  modulesPath,
  ubootFip,
  ...
}:

{
  imports = [ "${modulesPath}/installer/sd-card/sd-image.nix" ];

  nixpkgs.hostPlatform = "aarch64-linux";

  boot.loader.grub.enable = false;
  boot.loader.generic-extlinux-compatible.enable = true;

  # Explicit FDT line in extlinux.conf; this board reports "Hardkernel ODROID-N2Plus".
  hardware.deviceTree.name = "amlogic/meson-g12b-odroid-n2-plus.dtb";

  boot.kernelParams = [
    "console=ttyAML0,115200n8"
    "console=tty1"
    "consoleblank=0"
    # Carried over from the Armbian install: DMA pool for the Meson USB/MMC drivers, NCQ off for
    # the USB-SATA disks, and usb-storage (BOT instead of UAS) for the two Norelsys bridges
    # (0x2537:0x1066 / 0x2537:0x1068) that hang under UAS.
    "coherent_pool=2M"
    "libata.force=noncq"
    "usb-storage.quirks=0x2537:0x1066:u,0x2537:0x1068:u"
  ];

  # meson_ir: IR receiver -> /dev/lirc0 (rc-odroid keymap and protocol decoders load on demand).
  # meson_gxbb_wdt: SoC hardware watchdog fed by systemd (RuntimeWatchdogSec below).
  # The SAR ADC (meson-g12a-saradc, iio:device0) is built in (CONFIG_MESON_SARADC=y).
  boot.kernelModules = [
    "meson_ir"
    "meson_gxbb_wdt"
  ];

  systemd.settings.Manager.RuntimeWatchdogSec = "1min";

  sdImage = {
    # The FAT "FIRMWARE" partition is only meaningful for Raspberry Pi; it stays empty here.
    populateFirmwareCommands = "";
    populateRootCommands = ''
      mkdir -p ./files/boot
      ${config.boot.loader.generic-extlinux-compatible.populateCmd} -c ${config.system.build.toplevel} -d ./files/boot
    '';
    # Amlogic BootROM reads the boot image from sector 1 onward. Bytes 0..439 also come from the
    # blob; 440..511 (disk signature + partition table) must survive.
    postBuildCommands = ''
      blob=${ubootFip}/u-boot.bin.sd.bin
      gapBytes=$((${toString config.sdImage.firmwarePartitionOffset} * 1024 * 1024))
      if [ "$(stat -c %s "$blob")" -ge "$gapBytes" ]; then
        echo "u-boot.bin.sd.bin does not fit in front of the first partition" >&2
        exit 1
      fi
      dd if="$blob" of="$img" conv=notrunc bs=1 count=440
      dd if="$blob" of="$img" conv=notrunc bs=512 skip=1 seek=1
    '';
  };
}
