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

Exit codes: `0` converged, `2` usage, bad config or the wrong host (`hostname` is not `[modules.system] hostname`,
which every `machine.toml` must declare: a file copied to another box; `--force-host <current hostname>` overrides a
mismatch), `3` drift, `6` not root, `7` a module failed or was blocked by a failed module it `requires`, `130`/`143`
stopped by Ctrl-C/SIGTERM (passed on to the running module, which ends once its current command has: apt is never
cut off, the dotfiles installer is stopped; no further module runs). A failure never stops the run:
independent modules still converge. Everything except `list` needs root (`sudo`): without it, files such as
`/etc/sudoers.d/*` cannot be read and would show as drift.

## Layout

| Path | What |
|---|---|
| `machine.toml` | the declaration: one `[modules.<name>]` table per module; `enabled = false` switches one off |
| `install.sh` | the entry point (needs `python3` 3.11+, `jq`, `gpg` and `curl`; it names the missing one and the `apt-get` line) |
| `lib/engine.py` | reads `machine.toml`, runs the modules in `order`, honours `requires`, re-checks after install |
| `lib/common.sh` | module helpers: `ensure_file` (diff in preview, backup of foreign files, owner/mode), `drift`, `finish` |
| `modules/<name>/module.toml` | `description`, `order`, `requires` |
| `modules/<name>/run` | `check` (silent, exit 0/3), `plan` (prints the changes), `install` (root) — one code path for all three |
| `units/system/` | unit files and drop-ins the `systemd-units` module installs (`compose@.service`: one Compose stack per instance; the bots' services and timers) |
| `compose/<stack>/compose.yaml` | Compose stacks the `bots` module writes to `/srv/compose/<stack>/` (`cloudflared`) |
| `scripts/register-runner.sh` | one-time registration of a runner declared in `[modules.actions-runners]` (token on stdin) |

## Modules

| Order | Module | Declares |
|---|---|---|
| 10 | `system` | hostname (+ `127.0.1.1` in `/etc/hosts`), timezone |
| 20 | `apt-sources` | deb822 repositories; the signing key is written only if its fingerprint equals the declared one (Docker CE, Tailscale) |
| 25 | `service-users` | system users with fixed uids (`hsb`, `hcb-runner`, `hcb-qa`, and `hypercal` holding uid 999, the uid of HyperCalendarBot's container user, so that no package's system user gets it and with it the bot's data) |
| 30 | `packages` | `install` (missing ones, `--no-install-recommends`) and `purge` lists |
| 40 | `users` | users, groups, sudo rule (validated by `visudo`), exclusive `authorized_keys`, lingering for `systemd --user`; a new user whose declared login shell is not usable yet gets `nologin` (useradd's default with `sudo = "nopasswd"`) |
| 45 | `ssh-server` | sshd drop-in: no passwords, root only with a key, `AllowUsers` from `allow_users`; refuses while no allowed user with a key and a login shell (installed, in `/etc/shells`) can sudo (`nopasswd`, or `password` with a password set); `sshd -t` before reload |
| 50 | `zram` | zram swap: Armbian's `/etc/default/armbian-zram-config` (applied at next boot), else zram-tools |
| 55 | `sysctl` | `/etc/sysctl.d/90-debian-tweaks.conf`; live values are checked and set too |
| 60 | `docker` | the whole `/etc/docker/daemon.json` (validated by `dockerd --validate`), docker + containerd enabled and running |
| 68 | `bots` | pinned Bun runtimes (zip checked against its sha256) and global packages (pm2), directories, symlinks, one-rule sudoers files, Compose stack files |
| 70 | `systemd-units` | system units from `units/system`, user units from `units/user` (per user), enabled and started |
| 72 | `actions-runners` | the pinned runner release unpacked for each declared runner; once registered, its service enabled and running |
| 90 | `dotfiles` | `~/.files` of ultra applied by its `install-ubuntu.sh`; a link to the shared [`../dotfiles/debian`](../dotfiles/debian) (manual steps: [`../dotfiles/README.md`](../dotfiles/README.md)) |
| 95 | `login-shell` | login shells (`[modules.login-shell.shells]`, user = path), each set only once it is usable: installed and in `/etc/shells`, or `nologin`/`false`. Nothing requires it, so a shell that did not install (apt failed) fails only this module and `ssh-server` still turns off Armbian's password login, root's included |

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
3. From a clone of this repo, copy this directory and `machines/backup` (the `backup` module links to it) to the
   board and converge (after every later change, the same `rsync` + `--commit` re-applies it):

   ```sh
   ssh ultra@<ip> hostname   # Armbian's name for a fresh board; a known host's name (odroidn2, ...) is the wrong box
   # -L: modules/dotfiles is a symlink; modules/backup stays a link (its run finds hosts/ through it), copied apart
   rsync -aL --delete --exclude /modules/backup machines/bananapi-m4/ ultra@<ip>:debian-tweaks/
   rsync -a machines/bananapi-m4/modules/backup ultra@<ip>:debian-tweaks/modules/
   rsync -a --delete machines/backup/ ultra@<ip>:backup/
   ssh -t ultra@<ip> 'cd debian-tweaks && sudo ./install.sh --force-host <that name>'            # preview: read it
   ssh -t ultra@<ip> 'cd debian-tweaks && sudo ./install.sh --force-host <that name> --commit'
   ssh ultra@<ip> 'cd debian-tweaks && sudo ./install.sh check'   # exit 0
   ```

   `--force-host` only on this first run, and only with the name the board has now: until the `system` module names
   it `bananapi-m4`, the host guard refuses this `machine.toml` there. Every later run goes without it.

   The `ssh-server` module turns password login off, so make sure `ssh ultra@<ip>` works with a key first
   (`users` writes the keys from `machine.toml` in the same run, before `ssh-server`).
4. Reboot once so the zram sizes apply.

Not declared here (secrets or per-repo state): GitHub Actions runner registration tokens, the bots' checkouts, `.env`
files and data, the tunnel token in `/srv/compose/cloudflared/.env`, and the root-owned deploy wrappers the bot
repositories install by hand (HyperCalendarBot's `hypercal-activate-release` and stage QA, ExpenseSyncBot's stage QA).

## Bots

Since 2026-11-20 this board runs the Telegram bots that lived on the odroid (each repository's deploy docs have the
details and the move):

| Bot | Runs as | Where | Public name (tunnel `bananapi-m4`) | Runners |
|---|---|---|---|---|
| HyperSummaryBot | `hyper-summary-bot.service` (Bun 1.4.2, user `hsb`) | `/srv/hyper-summary-bot` | `hyper-summary-bot.mextner.com` → `:3003` | `bananapi-hsb` |
| HyperCalendarBot | Compose project `hypercalinvntrmru` (bot + redis), `compose@hypercalinvntrmru.service` | `/opt/hypercal` (`/srv/compose/hypercalinvntrmru` links to it) | `hypercal.mextner.com` → `:3001` | `bananapi-hcb`, `bananapi-hcb-qa` |
| ExpenseSyncBot | pm2 (`pm2-www-data.service`, Bun 1.2.17, user `www-data`), caddy | `/var/www/ExpenseSyncBot`, `/var/www/ExpenseSyncBot-stage` | `finbot*.mextner.com` → caddy `:80` | `bananapi-esb`, `bananapi-esb-qa` |

Their former cron jobs are the `esb-*` and `hcb-*` timers. Every unit waits (a condition, not a failure) until its
bot's own files are on the board, so a fresh install converges before any bot is restored. Memory: the board has
1.7 GB; the bot units and the Compose files cap their share, and every runner (with the jobs and deploys it runs, but
not the containers a job starts) shares one `runners.slice` (`MemoryHigh=700M` for all of them together).

One-time steps after a reinstall, in this order:

1. Tailscale: `sudo tailscale up --hostname bananapi-m4` and open the printed URL as the tailnet owner (remove a
   stale `bananapi-m4` machine in the admin console first, or the name gets a `-1`).
2. Tunnel: `printf 'TUNNEL_TOKEN=%s\n' "$token" | sudo install -m 600 /dev/stdin /srv/compose/cloudflared/.env`
   with the token of tunnel `bananapi-m4` (`cf tunnels token get <id>`), then `sudo ./install.sh --commit`.
3. Restore each bot's state (checkout, `.env`, data) as its repository describes; the units start on the next
   `--commit` (or `systemctl start`).
4. Runners: `sudo ./install.sh --only actions-runners --commit` unpacks them; register each with a fresh token
   (`gh` with admin rights on the repository), which also installs and starts its service:

   ```sh
   gh api -X POST repos/alex-mextner/HyperSummaryBot/actions/runners/registration-token --jq .token |
     ssh ultra@bananapi-m4 'cd debian-tweaks && sudo scripts/register-runner.sh bananapi-hsb'
   ```
