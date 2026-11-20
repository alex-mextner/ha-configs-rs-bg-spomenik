# machines: homelab hosts

Reproducible setup of every homelab machine, one directory per host. NixOS hosts are outputs of the single flake in
this directory (`machines/flake.nix`, one `flake.lock` for all of them). The Armbian host is described the same way
without Nix: a declaration file plus one idempotent entry point that converges the machine to it.

| Host | Hardware | OS | Role | Install / rebuild |
|---|---|---|---|---|
| [`macmini`](macmini/) | Mac mini Mid 2010 (Macmini4,1): Core 2 Duo P8600, 6 GB RAM, PNY CS900 240 GB SSD | NixOS 26.05 | headless server: Docker, Tailscale, mbpfan fan control | install: [`macmini/install-steps.md`](macmini/install-steps.md); rebuild on the host: `sudo nixos-rebuild switch --flake ./machines#macmini` |
| [`odroid-n2plus`](odroid-n2plus/) | Hardkernel ODROID-N2+: Amlogic S922X, 3.7 GB RAM, 64 GB eMMC | NixOS 26.05 (replaces the Armbian install, hostname `odroidn2`) | home server: Home Assistant + sidecars (Compose), LAN DHCP/DNS, Kodi on the projector, cloudflared, mextner.com, Tailscale, Anton's storage SFTP (sops secrets); move: [`odroid-n2plus/MIGRATION.md`](odroid-n2plus/MIGRATION.md) | image: `machines/odroid-n2plus/scripts/build-sd-image.sh`, see [`odroid-n2plus/README.md`](odroid-n2plus/README.md); rebuild: `nixos-rebuild switch --flake ./machines#odroid-n2plus --target-host ultra@odroidn2 --sudo` |
| [`rpi4`](rpi4/) | Raspberry Pi 4 Model B: BCM2711, 2 GB RAM, SD card, Wi-Fi | NixOS 26.05 | wall display: Chromium kiosk on Home Assistant with auto-login (sops secrets) | nightly `system.autoUpgrade` from `main`; image and manual deploy: [`rpi4/README.md`](rpi4/README.md) |
| [`bananapi-m4`](bananapi-m4/) | Banana Pi BPI-M4: Realtek RTD1395, 2 GB RAM, 8 GB eMMC | Armbian 26.11 (Debian 13 trixie), no Nix | the Telegram bots (HyperSummaryBot, HyperCalendarBot, ExpenseSyncBot), their GitHub Actions runners (arm64), cloudflared, Tailscale | Armbian image + first boot (the first run needs `--force-host`): [`bananapi-m4/README.md`](bananapi-m4/README.md); re-apply: the three `rsync` lines there (`modules/dotfiles` copied with `-L`, `modules/backup` kept a link to `machines/backup`), then `sudo ./install.sh --commit` there |

Every host also gets Alex's dotfiles (`~/.files` of `ultra`, zsh login shell): [`dotfiles/`](dotfiles/README.md), a
NixOS module imported by the NixOS hosts and a Debian module linked into `bananapi-m4`.

Every host backs up nightly to Google Drive with restic: [`backup/README.md`](backup/README.md) (what, schedule,
restore, one-time secrets).

Check every NixOS host from any machine with nix (evaluates everything, builds nothing):

```sh
nix flake check --no-build --all-systems ./machines
```

Flakes inside a git repo see only tracked files: `git add` a new file before checking, installing or rebuilding.
