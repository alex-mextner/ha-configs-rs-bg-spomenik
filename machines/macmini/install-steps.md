# macmini: install NixOS 26.05 on the Mac mini Mid 2010

The config lives in the `machines/` flake of
[alex-mextner/ha-configs-rs-bg-spomenik](https://github.com/alex-mextner/ha-configs-rs-bg-spomenik):

| File | Purpose |
|---|---|
| `machines/flake.nix` | `nixosConfigurations.macmini`, input `nixpkgs` = `nixos-26.05` |
| `machines/flake.lock` | pins nixpkgs to `f68407657ad294471eb4b2e65d193df26edf729f`, the same revision as the installer ISO |
| `machines/macmini/configuration.nix` | the system (headless, SSH keys only, Docker, Tailscale, mbpfan, zram) |
| `machines/macmini/hardware-configuration.nix` | hand-written, mounts by label (`BOOT`, `nixos`) |

Checked on ubuntu-pc (nix 2.36) against the locked nixpkgs:
`nix flake check --no-build` passes, and
`nix build ./machines#nixosConfigurations.macmini.config.system.build.toplevel --dry-run`
evaluates with no warnings (kernel 6.18.63, all big packages come from cache.nixos.org).
Nothing was built or booted.

The steps below wipe the whole internal SSD, including Linux Mint.

## 1. Write the installer to a USB stick (on ubuntu-pc)

ISO: `~/Downloads/nixos-minimal-26.05.14630.f68407657ad2-x86_64-linux.iso`
(sha256 `4a3f4d9dac89aa793a5632487f6358835160e5dca6691cab74b87e8f4dbb37b1`).

> **WARNING:** on ubuntu-pc, `/dev/sda` is the 931 GB backup disk. Never write to `/dev/sdX` names.
> Plug in the stick, then use its `/dev/disk/by-id/usb-…` name, and check it in `lsblk` first.

```sh
ls -l /dev/disk/by-id/ | grep usb-          # find the stick, ignore the -partN entries
lsblk -o NAME,SIZE,MODEL,TRAN               # confirm: TRAN=usb, size matches the stick
sudo dd if=~/Downloads/nixos-minimal-26.05.14630.f68407657ad2-x86_64-linux.iso \
  of=/dev/disk/by-id/usb-<STICK_ID> bs=4M conv=fsync oflag=direct status=progress
sync
```

## 2. Boot the Mac from the stick

1. Plug the Mac into the wired LAN (the built-in tg3 port; Wi-Fi is not used).
2. Power on and hold **Option (Alt)** until the boot picker appears.
3. Pick the orange **EFI Boot** entry, then the default NixOS entry.
4. You get a root-capable shell as user `nixos`. Run everything below with `sudo -i` first:

```sh
sudo -i
ip -4 addr                      # the Ethernet port should have a 192.168.0.x address
ping -c 2 cache.nixos.org
```

## 3. Partition and format the SSD

Check that `/dev/sda` is the PNY CS900 240 GB SSD and not the USB stick:

```sh
lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN
ls -l /dev/disk/by-id/ | grep -i pny
```

Use its by-id name (`ata-PNY_CS900_240GB_SSD_<SERIAL>`) for everything below:

```sh
DISK=/dev/disk/by-id/ata-PNY_CS900_240GB_SSD_<SERIAL>

wipefs -a "$DISK"
sgdisk --zap-all "$DISK"
parted -s "$DISK" -- \
  mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB \
  set 1 esp on \
  mkpart nixos ext4 513MiB 100%
udevadm settle

mkfs.fat -F 32 -n BOOT "${DISK}-part1"
mkfs.ext4 -F -L nixos "${DISK}-part2"
udevadm settle
ls -l /dev/disk/by-label/        # must show BOOT and nixos
```

The labels `BOOT` and `nixos` must match: `hardware-configuration.nix` mounts by these labels.

## 4. Mount

```sh
mount /dev/disk/by-label/nixos /mnt
mkdir -p /mnt/boot
mount -o umask=0077 /dev/disk/by-label/BOOT /mnt/boot
```

## 5. Clone the repo

The installer ISO has git. The repo is public, no credentials needed; submodules are not needed for the machines flake:

```sh
REPO=/mnt/etc/nixos/ha-configs-rs-bg-spomenik
git clone --depth 1 https://github.com/alex-mextner/ha-configs-rs-bg-spomenik "$REPO"
git -C "$REPO" log -1 --oneline     # the commit you are about to install
```

## 6. Compare with the detected hardware

```sh
nixos-generate-config --root /mnt --show-hardware-config > /tmp/hw-detected.nix
diff <(grep -A12 availableKernelModules /tmp/hw-detected.nix) \
     <(grep -A12 availableKernelModules "$REPO/machines/macmini/hardware-configuration.nix")
```

If `hw-detected.nix` lists an initrd module that is missing from `hardware-configuration.nix`, add it to
`$REPO/machines/macmini/hardware-configuration.nix` (and commit it to the repo later). The filesystem entries differ on
purpose (UUIDs vs labels); ignore that. Do not run `nixos-generate-config` without `--show-hardware-config`: the flake
does not read `/mnt/etc/nixos/configuration.nix`.

## 7. Install

```sh
NIX_CONFIG='experimental-features = nix-command flakes' \
  nixos-install --flake "$REPO/machines#macmini" --no-root-passwd
```

The clone is a git repo, so the flake sees only tracked files: `git -C "$REPO" add` any new file before installing.

`--no-root-passwd` leaves root without a password (root SSH login is disabled anyway). Set a password for `ultra`;
sudo and the local console need it, SSH does not:

```sh
nixos-enter --root /mnt -c 'passwd ultra'
```

Check that systemd-boot is on the firmware fallback path the Mac boots from:

```sh
ls -l /mnt/boot/EFI/BOOT/BOOTX64.EFI /mnt/boot/EFI/systemd/systemd-bootx64.efi
```

## 8. Reboot

```sh
umount -R /mnt
reboot
```

Remove the USB stick when the screen goes dark. If the Mac shows a flashing folder or waits a long time, hold
**Option** at power-on and pick **EFI Boot** on the internal disk.

## 9. After the first boot (from ubuntu-pc)

```sh
ssh -i ~/.ssh/id_ed25519_home ultra@<mac IP from the router's DHCP leases>
systemctl status mbpfan        # active (running), log shows the detected fan
sensors                        # applesmc + coretemp readings
cat /sys/devices/platform/applesmc.768/fan1_output   # target RPM set by mbpfan
sudo tailscale up              # log in to the tailnet
```

The fan should drop from full speed within a few seconds of mbpfan starting.

Rebuild after changing the config: push the change to the repo from any machine, then on the Mac (the clone is owned by
root, so pull as root):

```sh
cd /etc/nixos/ha-configs-rs-bg-spomenik
sudo git pull --ff-only
sudo nixos-rebuild switch --flake ./machines#macmini
```

Flakes in a git repo see only tracked files: `git add` new files before a rebuild.

## Notes

- **Bootloader on Apple EFI.** `boot.loader.efi.canTouchEfiVariables = false`: `bootctl` runs with
  `--no-variables` and writes nothing to the Mac's NVRAM, and it still installs `\EFI\BOOT\BOOTX64.EFI`, which the Mac
  firmware boots.
- **Kernel and mbpfan.** Linux 7.3 moved the applesmc fan files to the standard hwmon ABI
  (`fanN_output` → `hwmon/hwmonN/fanN_target`, `fanN_manual` → `pwmN_enable`). nixpkgs 26.05 ships mbpfan **2.4.0**,
  which only knows the old layout and fails with "could not detect any fan" on 7.3+. The fix
  ([linux-on-mac/mbpfan#276](https://github.com/linux-on-mac/mbpfan/pull/276), closed in favour of #271/#277/#278)
  shipped in mbpfan **v2.5.0** (2026-11-17), which is not in nixpkgs 26.05 yet. The 26.05 default kernel is 6.18.63
  LTS (the old ABI), so the config pins `pkgs.linuxPackages_6_18`. An assertion fails the build if someone moves to
  kernel ≥ 7.3 while mbpfan is still < 2.5.0. `linuxPackages_latest` in this nixpkgs is 7.3.4: do not use it until
  mbpfan is updated.
- **nouveau** stays loaded: it drives the local console for recovery. Add `"nouveau"` to
  `boot.blacklistedKernelModules` to disable it; the console then falls back to the EFI framebuffer.
- **CPU without SSE4.2/AVX (Core 2 Duo P8600).** nixpkgs x86_64-linux binaries target baseline x86-64, so the whole
  system runs. Watch out for prebuilt software that requires newer instruction sets: Docker images built on
  RHEL/Alma/Rocky/CentOS Stream 9 need x86-64-v2 (RHEL 10 needs v3); MongoDB ≥ 5 needs AVX; ClickHouse needs SSE4.2;
  most TensorFlow/ML wheels need AVX; Bun's default build needs AVX2 (a "baseline" build exists). [INFERENCE: from
  vendor requirements, not tested on this Mac.]
