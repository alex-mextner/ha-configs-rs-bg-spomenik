# USB disks. Everything the services need lives on the eMMC; the two USB disks are optional at boot (nofail).
# Only transmission (RequiresMountsFor ~/Downloads) waits for /mnt/wd500 at boot, up to the 300 s device timeout
# when the disk is missing; Anton's SFTP unit is started by its timer, never by the boot transaction.
#
# /mnt/storage: Anton's disk (Toshiba 3 TB). It holds his ciphertext and is only served by his SFTP unit
# (modules/anton.nix). Same line as in the Armbian fstab: an automount, never formatted or resized
# (autoFormat/autoResize stay off), no swap on it any more (Armbian had a swapfile there).
#
# /mnt/wd500: the WD 500 GB disk. Armbian ran /home/ultra, /var/lib/docker and the bots from it; after the move
# it holds the migration staging copy (MIGRATION.md) and the torrents, and becomes media-only (ticket #110).
{
  fileSystems."/mnt/storage" = {
    device = "/dev/disk/by-uuid/8da3f69e-6c2d-4028-8a0e-c344f0922fbb";
    fsType = "ext4";
    options = [
      "defaults"
      "noatime"
      "nosuid"
      "nodev"
      "noexec"
      "nofail"
      "x-systemd.automount"
      "x-systemd.device-timeout=300s"
    ];
  };

  fileSystems."/mnt/wd500" = {
    device = "/dev/disk/by-uuid/83c5bd3c-2cfa-494e-b0a5-17d3c92d0925";
    fsType = "ext4";
    options = [
      "noatime"
      "nofail"
      "x-systemd.device-timeout=300s"
    ];
  };

  # Transmission downloads to ~/Downloads (modules/media.nix); the data goes to the media disk, never to the eMMC.
  # Without the disk the bind is skipped and transmission does not start (RequiresMountsFor there).
  fileSystems."/home/ultra/Downloads" = {
    device = "/mnt/wd500/torrents";
    fsType = "none";
    options = [
      "bind"
      "nofail"
      "x-systemd.requires-mounts-for=/mnt/wd500"
    ];
  };
}
