# bananapi-m4: declarative setup (debian-tweaks style)

The Banana Pi BPI-M4 runs Armbian (Debian 13 trixie, kernel 6.18), not NixOS. This directory is its NixOS-like
spec: `machine.toml` declares the whole machine, `install.sh` converges the machine to it, and re-running is safe.
Modelled on [ubuntu-tweaks](https://github.com/alex-mextner/ubuntu-tweaks): one entry point, idempotent modules,
preview before change, observed convergence.

```sh
sudo ./install.sh              # preview: every change as a diff, nothing written (exit 3 while anything drifts)
sudo ./install.sh --commit     # apply; a second run prints "N ok" and exit 0
sudo ./install.sh check        # read-only, exit 0 converged / 3 drift (for a timer or monitoring)
./install.sh list              # modules in order, on/off
sudo ./install.sh --only docker,sysctl --commit
```

Exit codes: `0` converged, `2` usage, bad config or the wrong host (`hostname` is not `[modules.system] hostname`:
a `machine.toml` copied to another box; `--force-host <current hostname>` overrides it), `3` drift, `6` not root,
`7` a module failed or was blocked by a failed module it `requires`. A failure never stops the run: independent
modules still converge. Everything except `list` needs root (`sudo`): without it, files such as `/etc/sudoers.d/*`
cannot be read and would show as drift.

## Layout

| Path | What |
|---|---|
| `machine.toml` | the declaration: one `[modules.<name>]` table per module; `enabled = false` switches one off |
| `install.sh` | the entry point (needs `python3` 3.11+, `jq`, `gpg` and `curl`; it names the missing one and the `apt-get` line) |
| `lib/engine.py` | reads `machine.toml`, runs the modules in `order`, honours `requires`, re-checks after install |
| `lib/common.sh` | module helpers: `ensure_file` (diff in preview, backup of foreign files, owner/mode), `drift`, `finish` |
| `modules/<name>/module.toml` | `description`, `order`, `requires` |
| `modules/<name>/run` | `check` (silent, exit 0/3), `plan` (prints the changes), `install` (root) — one code path for all three |
| `units/system/` | unit files the `systemd-units` module installs (`compose@.service`: one Compose stack per instance) |

## Modules

| Order | Module | Declares |
|---|---|---|
| 10 | `system` | hostname (+ `127.0.1.1` in `/etc/hosts`), timezone |
| 20 | `apt-sources` | deb822 repositories; the signing key is written only if its fingerprint equals the declared one (Docker CE) |
| 30 | `packages` | `install` (missing ones, `--no-install-recommends`) and `purge` lists |
| 40 | `users` | users, groups, shell (set only once it is usable: in `/etc/shells`, or `nologin`/`false`; a failed shell blocks `ssh-server`), sudo rule (validated by `visudo`), exclusive `authorized_keys`, lingering for `systemd --user` |
| 45 | `ssh-server` | sshd drop-in: no passwords, no root login; refuses while no user with a key can sudo (`nopasswd`, or `password` with a password set); `sshd -t` before reload |
| 50 | `zram` | zram swap: Armbian's `/etc/default/armbian-zram-config` (applied at next boot), else zram-tools |
| 55 | `sysctl` | `/etc/sysctl.d/90-debian-tweaks.conf`; live values are checked and set too |
| 60 | `docker` | the whole `/etc/docker/daemon.json` (validated by `dockerd --validate`), docker + containerd enabled and running |
| 70 | `systemd-units` | system units from `units/system`, user units from `units/user` (per user), enabled and started |
| 90 | `dotfiles` | `~/.files` of ultra applied by its `install-ubuntu.sh`; a link to the shared [`../dotfiles/debian`](../dotfiles/debian) (manual steps: [`../dotfiles/README.md`](../dotfiles/README.md)) |

Every file a module writes starts with `# Managed by debian-tweaks (<module>)`. A file without that line is copied
to `/var/backups/debian-tweaks/` before it is replaced. Adding a module: a new `modules/<name>/` with `module.toml`
and an executable `run` that sources `lib/common.sh`; its options arrive as JSON in `DT_OPTIONS`.

## First boot of a fresh Armbian image

1. Download the image (Armbian community build, not stored in this repo) and check its sha256:

   ```sh
   IMG=Armbian_community_26.11.0-trunk.130_Bananapim4_trixie_current_6.18.63_minimal.img.xz
   curl -fLO "https://github.com/armbian/community/releases/download/26.11.0-trunk.130/$IMG"
   echo "fd1b440dc676aff7a8902e380f58fe2d68182179452e26d2f04f90aef232cfc6  $IMG" | sha256sum -c
   ```

   Write it to a microSD card (SW2 on the board's underside at 0), boot with Ethernet, find the board's address in
   the DHCP leases:

   ```sh
   xzcat "$IMG" | sudo dd of=/dev/disk/by-id/usb-<SD-READER-ID> bs=4M conv=fsync status=progress
   ```

   Move it to the eMMC later with `sudo armbian-install` (boot from eMMC, system on eMMC), power off, remove the
   card. On RTD1395 the installer puts U-Boot into a FAT partition 1 on the eMMC: the SPI loader reads it there.
2. `ssh root@<ip>`, password `1234`. Armbian's first-login asks for a new root password and creates the first
   user: create `ultra` (it becomes sudo-enabled). Optional unattended alternative: before the first boot write
   `/root/.not_logged_in_yet` on the card's root partition with `PRESET_USER_NAME=ultra`,
   `PRESET_USER_PASSWORD=...`, `PRESET_ROOT_PASSWORD=...`, `PRESET_USER_KEY=https://github.com/alex-mextner.keys`,
   `PRESET_TIMEZONE=Europe/Belgrade`, `PRESET_LOCALE=en_US.UTF-8` (variables read by
   `/usr/lib/armbian/armbian-firstlogin` on this image; passwords are secrets and are not part of this spec).
3. From a clone of this repo, copy this directory to the board and converge (after every later change, the same
   `rsync` + `--commit` re-applies it):

   ```sh
   ssh ultra@<ip> hostname   # Armbian's name for a fresh board; a known host's name (odroidn2, ...) is the wrong box
   rsync -aL --delete machines/bananapi-m4/ ultra@<ip>:debian-tweaks/   # -L: modules/dotfiles is a symlink
   ssh -t ultra@<ip> 'cd debian-tweaks && sudo ./install.sh --force-host <that name>'            # preview: read it
   ssh -t ultra@<ip> 'cd debian-tweaks && sudo ./install.sh --force-host <that name> --commit'
   ssh ultra@<ip> 'cd debian-tweaks && sudo ./install.sh check'   # exit 0
   ```

   `--force-host` only on this first run, and only with the name the board has now: until the `system` module names
   it `bananapi-m4`, the host guard refuses this `machine.toml` there. Every later run goes without it.

   The `ssh-server` module turns password login off, so make sure `ssh ultra@<ip>` works with a key first
   (`users` writes the keys from `machine.toml` in the same run, before `ssh-server`).
4. Reboot once so the zram sizes apply.

Not declared here (secrets or per-repo state): GitHub Actions runner registration tokens, the bots' `.env` files,
the contents of `/srv/compose/<stack>/`.
