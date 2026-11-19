# machines/backup: nightly encrypted backups to Google Drive

Every homelab host backs up its own state every night with [restic](https://restic.net) (encrypted, incremental,
deduplicated) through [rclone](https://rclone.org) to the owner's Google Drive (invntrm@gmail.com, 5 TB). Each host
has its own repository and its own restic password, so a lost password or a corrupted repository affects one host
only. Storage is shared: whoever is root on a host holds a Google token that can write (and delete) everything the
token's scope reaches, see Secrets for keeping that to the backup folders:

```
gdrive:homelab-backups/restic/<host>     one restic repository per host
gdrive:homelab-backups/tasks/<RUNID>/    off-site copy of the Tasks backups (already age-encrypted on bread)
```

| Path | What |
|---|---|
| `policy.toml` | shared by all hosts: remote, retention, check subset, schedules, pinned rclone version + sha256 |
| `hosts/<host>.toml` | what one host backs up: `paths`, `exclude`, `[[dumps]]`, `drill_file`, `notify`, `scope` |
| `lib/homelab-backup` | the one script: dumps, backup, retention, check, restore drill, status JSON, alerts |
| `lib/tasks_offsite.py` | Tasks off-site copy, uses the puller's own retention (`tasks_backup.common.select_keep`) |
| `nixos.nix` | NixOS module: `services.restic.backups.homelab` + weekly check + monthly drill |
| `debian/` | debian-tweaks module (`check`/`plan`/`install`) for Debian/Ubuntu: restic, rclone, units, timers |
| `tests/roundtrip.sh` | end-to-end test against a local repository (no Google Drive, no root) |

## What is backed up

Live SQLite files are never copied as they are: a consistent dump is made first (`VACUUM INTO` in one read
transaction, opened as the database's owner when run as root, checked with
`PRAGMA integrity_check`), and the live `*.db`, `-wal`, `-shm` files are excluded. Postgres: `pg_dump -Fc` through
`docker exec`. Redis: `BGSAVE`, then a copy of `/data` (RDB + AOF). Dumps go to `<state_dir>/staging` with a
`SHA256SUMS`, and that directory is part of every snapshot.

| Host | Path / database | Method | Size (Nov 2026) |
|---|---|---|---|
| odroidn2 | `~/homeassistant`: config, `.storage/`, `secrets.yaml`, `.env`, logs, `printops-backup/` (PrintOps + Spoolman snapshots of 04:15) | files | ~200 MB |
| odroidn2 | `home-assistant_v2.db`, `zigbee.db` | sqlite dump | ~445 MB |
| odroidn2 | `~/music-assistant` (settings, certs, logs) + `library.db`, `auth.db` | files + sqlite dump | ~30 MB |
| odroidn2 | `~/esphome` (YAML, components), `~/ssh-data`, `~/.config` (user units, Tasks keys) | files | ~10 MB |
| odroidn2 | ExpenseSyncBot `data/` (receipts, own backups), `logs/`, `.env`; `expenses.db` | files + sqlite dump | ~160 MB |
| odroidn2 | HyperSummaryBot `data/`, `logs/`, `.env`; `hyper-summary.db` | files + sqlite dump | ~125 MB |
| odroidn2 | HyperCalendarBot `/opt/hypercal` `.env`, compose, `data/`, `logs/`; `calendar.db`; redis `hypercal-redis` | files + sqlite dump + BGSAVE | ~40 MB |
| odroidn2 | Postgres `hga` (container `homeassistant-hga-postgres-1`) | pg_dump | small |
| odroidn2 | `/etc`, `/var/spool/cron`, `/var/lib/nixos` (after the NixOS reinstall) | files | ~5 MB |
| ubuntu-pc | Postgres `quasar`, `quasar_stage` (container `aisis-yandex-postgres-1`) | pg_dump | ~16 MB |
| ubuntu-pc | `~/.config/tasks-backup` (age key!), `~/.config/tg-cli`, `~/.ssh`, aisis `compose.yaml` | files | <1 MB |
| ubuntu-pc | Tasks backup sets from the puller → `gdrive:homelab-backups/tasks` | rclone copy, hourly | ~390 MB |
| bananapi-m4 | `/etc`, `/var/spool/cron`, `/srv/compose` (bots stack), `~/.config` | files | small (no stack yet) |
| macmini | `/var/lib/nixos`, `/srv`, `/home/ultra` | files | small (fresh install) |

Not backed up: models and caches (`.cache`, `.ollama`, `node_modules`, ESPHome build dirs, docker images), git
checkouts that live on GitHub (`homeassistant/.git`, `submodules/`), Home Assistant's own backup tarballs (the same
data), torrents, the data on `/mnt/wd500` that is not listed above, and **never** Anton's `/mnt/storage` or
`/home/anton`. Expect ~1 GB for the first odroidn2 upload, then tens of MB per night.

## Schedule and retention

All times Europe/Belgrade, from `policy.toml`; `Persistent=true` catches up after downtime.

| When | What |
|---|---|
| daily 04:00 + up to 10 min | dumps, `restic backup`, `restic forget --prune --group-by host --keep-daily 7 --keep-weekly 4 --keep-monthly 12` |
| Sundays 05:30 | `restic check --read-data-subset=5%` |
| 1st of the month 06:30 | restore drill: restore the dump dir and `drill_file` from the latest snapshot into a temp dir with `--verify`, `sha256sum -c` of the dumps, `PRAGMA integrity_check` of every SQLite dump, `PGDMP` header of every pg dump, `cmp` of `drill_file` with the live file (when it has not changed since) |
| hourly :35 (ubuntu-pc) | Tasks off-site copy; remote retention = the puller's (12 newest + one per day for 30 days) |

Each run writes `<state_dir>/status-<verb>.json` (`ok`, `message`, times): `/var/lib/homelab-backup` for system
hosts, `~/.local/state/homelab-backup` on ubuntu-pc. A failure also runs the host's `notify` command: on ubuntu-pc
that is `tg --tag problem`. The other hosts have no Telegram CLI: there the status JSON and the systemd journal are
the signal (`homelab-backup status`, `systemctl list-timers 'homelab-backup*' 'restic-backups-*'`).

## Install

**NixOS** (macmini, odroid-n2plus): the flake host imports `./backup/nixos.nix` and sets
`homelab.backup.enable = true;` (plus `homelab.backup.host = "odroidn2";` on the ODROID, whose host file is shared
with its Armbian install). `sudo nixos-rebuild switch --flake ./machines#<host>`, then the secrets below.

**bananapi-m4**: the module is `modules/backup` there (a symlink to `../../backup/debian`), configured by
`[modules.backup]` in `machine.toml`; it converges with the rest: `sudo ./install.sh --commit`. The symlink needs
`machines/backup` next to the board's copy of `machines/bananapi-m4`: deploy both (see the bananapi-m4 README).

**Other Debian/Ubuntu hosts** (ubuntu-pc, odroidn2 until the reinstall):

```sh
machines/backup/debian/install.sh ubuntu-pc              # preview; scope = "user": as ultra, no sudo
machines/backup/debian/install.sh ubuntu-pc --commit
sudo machines/backup/debian/install.sh odroidn2 --commit # scope = "system"
```

The module installs restic (apt; on a `scope = "user"` host it must already be installed), rclone
v1.76.0 (static build, sha256 checked) to `/usr/local/bin` or `~/.local/bin`, `homelab-backup` + `config.json`, and
enables `homelab-backup.timer`, `homelab-backup-check.timer`, `homelab-backup-drill.timer` (and
`homelab-backup-tasks.timer` where `[tasks_offsite]` is set). A second `--commit` changes nothing.

## Secrets (one time per host, by the owner)

Neither the restic password nor the Google token is ever in git. Per host:

| | system hosts | ubuntu-pc |
|---|---|---|
| restic password | `/etc/homelab-backup/restic-password` (root, 0600) | `~/.config/homelab-backup/restic-password` (0600) |
| rclone config with the `[gdrive]` remote | `/etc/homelab-backup/rclone.conf` (root, 0600) | `~/.config/rclone/rclone.conf` (existing) |

> **Keep every host's restic password offline as well** (password manager and paper). The repository is encrypted
> with it: if the host dies and the password was only on that host, its backups cannot be read by anyone.

1. **Google Drive OAuth client** (once, in a browser). rclone's shared client id is being retired during 2026 (rclone
   1.76 warns about it), so create your own: Google Cloud Console → new project → enable "Google Drive API" → OAuth
   consent screen (External, add invntrm@gmail.com as test user, then "Publish app" so the token does not expire
   after 7 days) → Credentials → OAuth client ID, type "Desktop app". Keep the client id and secret
   (https://rclone.org/drive/#making-your-own-client-id).
2. **On the host** (headless), the remote with scope `drive.file`: the token then reaches only files this OAuth
   client created (the backup folders), not the rest of the Drive. Interactive, so neither the client secret nor
   the token lands in shell history, `ps` or sudo's log:

   ```sh
   sudo install -d -m 700 /etc/homelab-backup
   sudo rclone config --config /etc/homelab-backup/rclone.conf
   #   n (new remote) → name: gdrive → storage: drive → client_id / client_secret from step 1
   #   → scope: drive.file → service_account_file: empty → advanced config: n
   #   → "Use web browser to automatically authenticate?": n  → it prints `rclone authorize "drive" "<blob>"`:
   #     run that on ubuntu-pc (opens the browser, prints a token) and paste the token at the prompt
   #   → shared drive: n → keep the remote: y → q
   sudo chmod 600 /etc/homelab-backup/rclone.conf
   sudo rclone about gdrive: --config /etc/homelab-backup/rclone.conf          # must print the 5 TiB quota
   ```

3. **Restic password**, then the repository:

   ```sh
   sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 > /etc/homelab-backup/restic-password'
   sudo cat /etc/homelab-backup/restic-password                               # → password manager + paper
   sudo homelab-backup init        # NixOS: in PATH; Debian: /usr/local/lib/homelab-backup/homelab-backup init
   ```

   ubuntu-pc: the `[gdrive]` remote already exists (scope `drive`, rclone's shared client id; `rclone about gdrive:`
   works). Move it to your own client id: `rclone config update gdrive client_id <id> client_secret <secret> scope
   drive.file`, then `rclone config reconnect gdrive:`. Note that a `drive.file` token sees only files created by
   its own client id: the PC repository written so far under rclone's shared id becomes invisible to it, so after
   the switch move the old `homelab-backups/restic/ubuntu-pc` aside in the Drive web UI and let the next run start
   a fresh repository (or keep scope `drive`). The PC's password was created once with
   `umask 077; head -c 32 /dev/urandom | base64 > ~/.config/homelab-backup/restic-password`: copy it offline.

## Restore

Every command below runs as the backup user (root on system hosts) with the host's settings:
`homelab-backup restic ...` is plain restic with the repository, password and rclone config already set. On NixOS
the `restic-homelab` wrapper does the same.

```sh
homelab-backup snapshots                                   # list this host's snapshots
homelab-backup restic ls latest /home/ultra/homeassistant  # browse
# restore into a scratch dir first, never straight over live data
homelab-backup restic restore latest --target /var/tmp/restore --include /home/ultra/homeassistant/.storage --verify
homelab-backup restic restore <snapshot-id> --target /var/tmp/restore --include /var/lib/homelab-backup/staging
diff -r /var/tmp/restore/home/ultra/homeassistant/.storage /home/ultra/homeassistant/.storage
```

Databases from the restored staging dir (stop the application first):

```sh
cp /var/tmp/restore/var/lib/homelab-backup/staging/ha-recorder.sqlite /home/ultra/homeassistant/home-assistant_v2.db
rm -f /home/ultra/homeassistant/home-assistant_v2.db-wal /home/ultra/homeassistant/home-assistant_v2.db-shm
docker exec -i homeassistant-hga-postgres-1 pg_restore -U hga -d hga --clean --if-exists \
  < /var/tmp/restore/var/lib/homelab-backup/staging/hga.pgdump
# redis: stop the container, copy staging/hypercal-redis/* into its /data volume, start it
```

A whole host from a machine that has none of its files, given the password and an rclone config:

```sh
export RESTIC_REPOSITORY=rclone:gdrive:homelab-backups/restic/odroidn2 RESTIC_PASSWORD_FILE=./restic-password
export RCLONE_CONFIG=./rclone.conf
restic snapshots
restic restore latest --target /mnt/newroot --verify
```

Tasks sets: `rclone copy gdrive:homelab-backups/tasks/<RUNID> ./<RUNID>`, then the Tasks restore procedure
(`python3 -m tasks_backup.drill` decrypts them with `~/.config/tasks-backup/age.key`, itself in the ubuntu-pc
repository).

## Test

```sh
machines/backup/tests/roundtrip.sh   # local restic repo in /var/tmp; exit 0 = all checks passed
shellcheck -x machines/backup/lib/homelab-backup machines/backup/debian/run machines/backup/debian/install.sh machines/backup/tests/roundtrip.sh
nix flake check --no-build ./machines
```

The round trip covers: dumps of live WAL databases (a row that is only in the WAL is in the dump), the snapshot,
excludes, a full restore with an empty `diff -r` and identical SQL content, the drill, a second run that adds no
data, retention on backdated snapshots, `check`, a failing dump (status + notify, the snapshot still written), a
missing password file, and the Tasks copy with the puller's retention against a local rclone remote.
