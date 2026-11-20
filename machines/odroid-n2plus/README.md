# odroid-n2plus: NixOS 26.05 on the Hardkernel ODROID-N2+

The home server `odroidn2` (Amlogic S922X / G12B, 3.7 GB RAM, 64 GB eMMC) that replaces the Armbian install:
Home Assistant and its sidecars, the LAN's DHCP and DNS, Kodi on the HDMI projector, the Cloudflare Tunnel, and
Anton's storage. Static `192.168.0.39/24` on `end0`, SSH with keys only. The move from Armbian, with rollback:
[`MIGRATION.md`](MIGRATION.md). `docs/network.md` and `AGENTS.md` still give 192.168.0.39 to the HA PC; that is
stale: the ODROID holds .39.

| Path | What |
|---|---|
| `../flake.nix` | `nixosConfigurations.odroid-n2plus` and the `odroid-n2plus-*` packages |
| `configuration.nix` | network, SSH, users `ultra` (1000) and the module list, zram, Nix settings |
| `hardware/odroid-n2plus.nix` | board support on the stock nixpkgs aarch64 kernel, extlinux boot, SD image layout |
| `modules/nic-alive.nix` | reboots when the on-board NIC has carrier but receives nothing for 10 minutes |
| `modules/storage.nix` | Anton's disk `/mnt/storage` (automount, never formatted), the WD500 `/mnt/wd500`, `~/Downloads` on it |
| `modules/memory.nix` | `docker.slice` and the MemoryMax of the nixpkgs services (budget in the file) |
| `modules/home-assistant.nix` | Docker, BlueZ, `ha-compose`, `ha-host-actions`, `ha-mdns`, `ha-soil-moisture`, HA fail-safe timers |
| `ha/compose.odroid.yaml` | this board's Compose overlay for the HA stack (image pins, memory limits, ha-net, macvlan parent) |
| `modules/home-lan.nix` | `home-lan` Compose project: DHCP (dnsmasq), DNS relay, TLS front for the Yandex speakers |
| `modules/printops.nix` | PrintOps + Spoolman Compose project (interim, moves to the Mac mini) |
| `modules/media.nix` | Kodi on demand (kodi-gbm on HDMI, X11 build in Xvfb headless), Transmission, Glances |
| `modules/edge.nix` | cloudflared (remotely managed tunnel), caddy for mextner.com, its GitHub deploy runner |
| `modules/ultra-timers.nix` | ultra's periodic jobs: PrintOps snapshot, router CLI, Tasks backup pull and drill |
| `modules/anton.nix` | user `anton` (1002), his userspace tailscaled, `rclone serve sftp /mnt/storage` on 127.0.0.1:2224, his benchmark |
| `modules/secrets.nix`, `secrets/secrets.sops.yaml`, `.sops.yaml` | sops-nix: encrypted secrets and their recipients |
| `pkgs/uboot-odroid-n2.nix` | mainline U-Boot (`odroid-n2_defconfig`), the BL33 stage |
| `pkgs/uboot-odroid-n2plus-fip.nix` | signed boot blob `u-boot.bin.sd.bin`: Amlogic BL2/BL30/BL31 from LibreELEC/amlogic-boot-fip + U-Boot |
| `scripts/build-sd-image.sh` | builds the image on an x86_64 host with Docker only (no system Nix, no host binfmt) |

Boot chain: Amlogic BootROM → BL2/BL30/BL31 (vendor blobs) → mainline U-Boot → `/boot/extlinux/extlinux.conf` on the
ext4 root partition.

## What runs here

| Service | How | Ports | Data |
|---|---|---|---|
| Home Assistant 2026.9.3 | Compose `homeassistant` (`ha-compose.service`), host network, privileged | 8123, 21064-21065 (HomeKit) | `~/homeassistant` (the HA config checkout: `.storage`, `home-assistant_v2.db`, `zigbee.db`, `secrets.yaml`, `.env`) |
| Music Assistant 2.10.4 | Compose, host network, root (AirPlay PTP) | 8095, 8097, 319-320/udp | `~/music-assistant` |
| ESPHome | Compose | 6052 | `~/esphome` |
| HGA Postgres (pgvector 17) | Compose, ha-net only | - | `~/hga-postgres` (uid 999) |
| go2rtc, u1-camera-proxy, printer-proxy | Compose (3d-cli include) | 1984, 8554, 8555 | `~/odroid-overlay/go2rtc.yaml` |
| avahi-reflector, ha-docker-dns (CoreDNS of ha-net), bgutil-pot, ssh-server | Compose | 2222, 127.0.0.1:4416 | `~/ssh-data` |
| DHCP (dnsmasq), DNS relay, TLS front | Compose `home-lan` | 67/udp, 192.168.0.39:53, 192.168.0.39:443 | `~/.local/share/home-lan` (config, static leases), `~/.local/state/home-lan/dhcp/dnsmasq.leases` |
| PrintOps API, Spoolman | Compose `printops` | 8765, 7912 | `~/printops/data` |
| HA host action bridge | `ha-host-actions.service` (ultra) | 172.20.0.1:8787 | `~/.local/bin/ha-host-actions.py`, `~/.config/ha-host-actions` |
| mDNS publisher | `ha-mdns.service` (ultra) | 5353/udp | - |
| Soil probe ADC bridge | `ha-soil-moisture.service` (root, /dev/gpiochip0 only) | 127.0.0.1:8768 | `scripts/soil_moisture/soil_bridge.py` of this repository |
| IR receiver | kernel `meson_ir` → `/dev/lirc0`, read by HA (`ekf_appliances` native IR); no lircd | - | - |
| Kodi | `kodi.service` on demand (ha-host-actions via sudo), HDMI via GBM | 8080 (JSON-RPC) | `~/.kodi`, `~/KodiLibrary` |
| Transmission 4 | `transmission.service` (nixpkgs module, as ultra) | 9091, 51413 | `~/.config/transmission-daemon`, downloads on `/mnt/wd500/torrents` |
| Glances | `glances.service` (nixpkgs module) | 127.0.0.1:61208 | - |
| Cloudflare Tunnel | `cloudflared.service`, token from sops | 127.0.0.1:20241 (metrics) | ingress rules in the Cloudflare dashboard |
| mextner.com origin | `caddy-mextner.service` | 127.0.0.1:8081 | `/var/www/mextner.com`, Caddyfile from the site repo |
| mextner.com deploy runner | `github-runner-odroid-mextner.service` (nixpkgs module) | - | `/var/lib/github-runner/odroid-mextner` |
| Tailscale (ultra's tailnet) | `tailscaled.service` | 41641/udp, tailscale serve 443 | `/var/lib/tailscale` |
| Anton: tailscaled | `tailscaled-anton.service` (anton, userspace) | 41643/udp | `/home/anton/.local/state/ts-owner` |
| Anton: SFTP of his disk | `anton-storage-sftp.service` + timer (anton) | 127.0.0.1:2224 | `/mnt/storage` (never read by anything else), `/home/anton/.config/storage-sftp` |
| Backups | `homelab-backup*.timer` (`../backup`, host file `odroidn2-nixos.toml`) | - | Google Drive `homelab-backup-odroidn2` |

The bots (HyperCalendarBot + redis, ExpenseSyncBot, HyperSummaryBot) and their CI runners run on bananapi-m4, not
here. Spoolman/PrintOps and Transmission are interim (Mac mini later).

Native NixOS modules are used where the service's configuration can live in Nix: Transmission, Glances,
Tailscale, the GitHub runner, BlueZ. Not where another owner edits the config at run time: dnsmasq (HA's host
action bridge edits the static leases and restarts the container), caddy for mextner.com (the site repository
deploys its Caddyfile), cloudflared (remotely managed tunnel; nixpkgs' module is for locally managed tunnels only),
avahi (the HA stack's reflector relays three service types into ha-net through its own macvlan leg).

## Memory

3.7 GB of RAM plus zram (50%, zstd); no swap on disk (Armbian froze in a swap thrash on its USB swapfile). Every
service has a MemoryMax: the units in `modules/*.nix` next to their definition, the nixpkgs units and the slices in
`modules/memory.nix`. All containers share `docker.slice` (MemoryHigh 2400M, MemoryMax 2700M) and each has its own
Compose limit (`ha/compose.odroid.yaml`, `home-lan.nix`, `printops.nix`). Check:
`systemctl show docker.slice -p MemoryCurrent,MemoryMax`, `docker stats --no-stream`, `cat /proc/pressure/memory`.

## Secrets

In git, encrypted (sops-nix, `secrets/secrets.sops.yaml`, recipients in `.sops.yaml`: the owner's admin age key and
the host's SSH host key):

| Secret | Used by | Source |
|---|---|---|
| `cloudflared-token` | `cloudflared.service` | the tunnel token from Armbian's `/etc/cloudflared/token` (already encrypted in this file) |
| `transmission-settings` | `transmission.service` (merged over `settings.json`) | JSON `{"rpc-whitelist": ...}`: the Armbian whitelist including the host's tailnet address, which must not be in this public repository (already encrypted) |

Edit with `cd machines/odroid-n2plus && sops secrets/secrets.sops.yaml` (needs the admin key in
`~/.config/sops/age/keys.txt`); `.githooks/pre-commit` refuses the file unless every value is ciphertext. The host
decrypts with `/etc/ssh/ssh_host_ed25519_key`, the Armbian install's key carried over by MIGRATION.md; a new host key
needs `ssh-to-age`, a new recipient in `.sops.yaml` and `sops updatekeys secrets/secrets.sops.yaml`.

Provisioned by hand on the host, never in git:

| What | Where | How |
|---|---|---|
| restic password, rclone `[gdrive]` remote | `/etc/homelab-backup/restic-password`, `/etc/homelab-backup/rclone.conf` | `../backup/README.md`, Secrets, "Reinstalling a host that already has a repository" (same password and Google project as the Armbian install) |
| GitHub runner registration token (valid 1 h, used only for the first registration; keep the file, the runner is skipped without it) | `/var/lib/github-runner-tokens/odroid-mextner` (root, 0600) | MIGRATION.md, step 8 |

Carried over as data by MIGRATION.md (they are application state, not configuration): Home Assistant's
`secrets.yaml`, `.env` (HGA_DB_PASSWORD, SSH_SERVER_PASSWORD) and `.storage`; `~/.env` (HA_TOKEN of the watchdogs);
`~/.config/ha-host-actions/{token,kodi.env}`; the home-lan private config; PrintOps token files; the tailscale node
state of both instances; Anton's SFTP host key and authorized_keys; the SSH host keys.

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

The image carries the whole configuration, but a fresh image has none of the data: install it with MIGRATION.md.

## Flash and boot

Write the image to a microSD card or the eMMC module (in a USB adapter). Use the `/dev/disk/by-id/` name and check it with
`lsblk` first; on ubuntu-pc `/dev/sda` is the backup disk.

```sh
lsblk -o NAME,SIZE,MODEL,TRAN
zstd -dc ~/.cache/odroid-n2plus-image/nixos-image-sd-card-*.img.zst \
  | sudo dd of=/dev/disk/by-id/usb-<CARD-ID> bs=4M conv=fsync status=progress
```

The image carries U-Boot in front of the first partition (bytes 0..439 and sector 1 onward), so the board boots it
directly. The root partition grows to the card size on first boot. The Armbian install on the same board also uses
192.168.0.39 and serves the LAN's DHCP: run one of the two at a time.

## Rebuild

The board has 3.7 GB of RAM shared with Home Assistant: evaluating nixpkgs on it takes ~1 GB. While the services run,
build elsewhere and copy the closure. From any machine with Nix and aarch64 support (on ubuntu-pc: inside the
`nixos/nix` container of `scripts/build-sd-image.sh`):

```sh
nixos-rebuild switch --flake ./machines#odroid-n2plus --target-host ultra@odroidn2 --sudo
```

On the board itself (fine when it is idle, e.g. during MIGRATION.md), straight from GitHub:

```sh
sudo nixos-rebuild switch --refresh \
  --flake 'github:alex-mextner/ha-configs-rs-bg-spomenik?dir=machines#odroid-n2plus'
```

The configuration name differs from the hostname (`odroidn2`), so `#odroid-n2plus` is always required. A rebuild never
restarts the Compose stacks: a changed overlay or limits file is applied with `docker compose up -d` (reload).
