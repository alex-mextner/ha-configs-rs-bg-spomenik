# odroid-n2plus: NixOS 26.05 on the Hardkernel ODROID-N2+

Headless server that will take over from the Armbian install on the ODROID-N2+ (Amlogic S922X / G12B): Docker, Tailscale,
SSH with keys only. Hostname `odroidn2-nixos`, static `192.168.0.39/24` on `end0` (the address the Armbian install uses).
`docs/network.md` and `AGENTS.md` still give 192.168.0.39 to the HA PC; that is stale: the ODROID holds .39 today.

| Path | What |
|---|---|
| `../flake.nix` | `nixosConfigurations.odroid-n2plus` and the `odroid-n2plus-*` packages |
| `configuration.nix` | the system: network, SSH, user `ultra`, Docker, Tailscale, zram |
| `hardware/odroid-n2plus.nix` | board support on the stock nixpkgs aarch64 kernel, extlinux boot, SD image layout |
| `modules/nic-alive.nix` | reboots when the on-board NIC has carrier but receives nothing for 10 minutes |
| `pkgs/uboot-odroid-n2.nix` | mainline U-Boot (`odroid-n2_defconfig`), the BL33 stage |
| `pkgs/uboot-odroid-n2plus-fip.nix` | signed boot blob `u-boot.bin.sd.bin`: Amlogic BL2/BL30/BL31 from LibreELEC/amlogic-boot-fip + U-Boot |
| `scripts/build-sd-image.sh` | builds the image on an x86_64 host with Docker only (no system Nix, no host binfmt) |

Boot chain: Amlogic BootROM → BL2/BL30/BL31 (vendor blobs) → mainline U-Boot → `/boot/extlinux/extlinux.conf` on the
ext4 root partition.

## Build the SD/eMMC image

The signing tool `aml_encrypt_g12b` exists only for x86_64, so the image needs an x86_64 host plus aarch64 (native or
emulated). On ubuntu-pc, from a clone of this repo:

```sh
machines/odroid-n2plus/scripts/build-sd-image.sh            # output: ~/.cache/odroid-n2plus-image/
```

The script runs Nix in a privileged `nixos/nix` container with a private binfmt_misc mount (Linux ≥ 6.7), so the host's
binfmt registrations are never touched; the Nix store stays in the Docker volume `nix-odroid-n2plus`. It writes
`nixos-image-sd-card-<version>-aarch64-linux.img.zst` and its `.sha256` next to it. On a host with Nix and aarch64
support the same image is `nix build ./machines#packages.x86_64-linux.odroid-n2plus-sdImage`.

The image is not stored in git. The build of the config in this directory produced
`nixos-image-sd-card-26.05.20261116.f684076-aarch64-linux.img.zst` with sha256
`c358bc94b34934fc8420e88b3eb19242a717215b2f72cdbaeb6c93a6ba9dd544`.

## Flash and boot

Write the image to a microSD card or the eMMC module (in a USB adapter). Use the `/dev/disk/by-id/` name and check it with
`lsblk` first; on ubuntu-pc `/dev/sda` is the backup disk.

```sh
lsblk -o NAME,SIZE,MODEL,TRAN
zstd -dc ~/.cache/odroid-n2plus-image/nixos-image-sd-card-*.img.zst \
  | sudo dd of=/dev/disk/by-id/usb-<CARD-ID> bs=4M conv=fsync status=progress
```

The image carries U-Boot in front of the first partition (bytes 0..439 and sector 1 onward), so the board boots it
directly. The root partition grows to the card size on first boot. Then:

```sh
ssh -i ~/.ssh/id_ed25519_home ultra@192.168.0.39
sudo tailscale up
```

The Armbian install on the same board also uses 192.168.0.39 and serves the LAN's DHCP: run one of the two at a time.

## Rebuild

On the board, straight from GitHub (no clone needed; `--refresh` fetches the latest commit):

```sh
sudo nixos-rebuild switch --refresh \
  --flake 'github:alex-mextner/ha-configs-rs-bg-spomenik?dir=machines#odroid-n2plus'
```

From a local clone: `sudo nixos-rebuild switch --flake ./machines#odroid-n2plus`. The configuration name differs from the
hostname, so `#odroid-n2plus` is always required.
