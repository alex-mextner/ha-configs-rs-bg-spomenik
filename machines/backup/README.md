# machines/backup: nightly encrypted backups to Google Drive

Every homelab host backs up its own state every night with [restic](https://restic.net) (encrypted, incremental,
deduplicated) through [rclone](https://rclone.org) to the owner's Google Drive (invntrm@gmail.com, 5 TB). Each host
has its own repository, its own restic password and its own Google token, from its own Google Cloud project with
scope `drive.file`: that token reaches only the folder the host created, so a host that is broken into (bananapi-m4
runs GitHub Actions jobs) can delete its own backups but no other host's (Secrets):

```
gdrive:homelab-backup-<host>/restic     the host's restic repository
gdrive:homelab-backup-ubuntu-pc/tasks/<RUNID>/   off-site copy of the Tasks backups (already age-encrypted on bread)
```

| Path | What |
|---|---|
| `policy.toml` | shared by all hosts: remote, folder prefix, retention, check subset, schedules, pinned restic + rclone versions and sha256 |
| `hosts/<host>.toml` | what one host backs up: `paths`, `exclude`, `[[dumps]]`, `drill_file`, `notify`, `scope`, `watch` |
| `lib/homelab-backup` | the one script: dumps, backup, retention, check, restore drill, watch of other hosts, status JSON, alerts |
| `lib/tasks_offsite.py` | Tasks off-site copy, uses the puller's own retention (`tasks_backup.common.select_keep`) |
| `nixos.nix` | NixOS module: the same `homelab-backup` verbs on the same timers |
| `debian/` | debian-tweaks module (`check`/`plan`/`install`) for Debian/Ubuntu: restic, rclone, units, timers |
| `tests/` | `roundtrip.sh` (the script against a local repository), `module.sh` (debian/ idempotency), `in-docker.sh` (both as root) |

## What is backed up

Live SQLite files are never copied as they are: a consistent dump is made first (`VACUUM INTO` in one read
transaction, checked with `PRAGMA integrity_check`), and the live `*.db`, `-wal`, `-shm` files are excluded. As
root, SQLite hands the `-wal`/`-shm` files it creates to the database's owner, so the application keeps working.
Postgres: `pg_dump -Fc` through `docker exec`. Redis: `BGSAVE`, then a copy of `/data` (RDB + AOF). Every dump is
built in a work dir under `<state_dir>` (a SQLite dump only after checking there is room for the database plus its
WAL), removed whatever the outcome, and moves to `<state_dir>/staging` only once it checked out; that directory,
with a `SHA256SUMS`, is part of every snapshot. Temp files (restic's packs, SQLite, rclone) go to `<state_dir>/tmp`:
`/tmp` is RAM on Armbian. One `run` or `dump` at a time (a lock in `<state_dir>`).

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
| ubuntu-pc | Tasks backup sets from the puller → `gdrive:homelab-backup-ubuntu-pc/tasks` | rclone copy, hourly | ~390 MB |
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
| daily 04:00 + up to 10 min | dumps, `restic backup --tag nightly`, `restic forget --tag nightly --prune --group-by host --keep-daily 7 --keep-weekly 4 --keep-monthly 12` (snapshots made by hand are never pruned) |
| Sundays 05:30 | `restic check --read-data-subset=5%` |
| 1st of the month 06:30 | restore drill: restore the dump dir and `drill_file` from the latest nightly snapshot into a temp dir with `--verify`, `sha256sum -c` of the dumps, `PRAGMA integrity_check` of every SQLite dump, `PGDMP` header of every pg dump, `cmp` of `drill_file` with the live file (when it has not changed since) |
| hourly :35 (ubuntu-pc) | Tasks off-site copy; remote retention = the puller's (12 newest + one per day for 30 days) |
| daily 09:00 (ubuntu-pc, when `watch` lists hosts) | watch: each listed host has one Drive folder, a snapshot and a finished run under 36 h old, and no failed verb |

Each run writes `<state_dir>/status-<verb>.json` (`ok`, `message`, times): `/var/lib/homelab-backup` for system
hosts, `~/.local/state/homelab-backup` on ubuntu-pc, and copies it to `<drive folder>/status/`. A failure also runs
the host's `notify` command: on ubuntu-pc that is `tg --tag problem`. The other hosts have no Telegram CLI, so
ubuntu-pc watches them every morning: for each host in `watch` (`hosts/ubuntu-pc.toml`) its full-scope token checks
that there is exactly one `homelab-backup-<host>` folder, that the newest snapshot file and the last `run` status are
under 36 hours old, and that none of the host's uploaded statuses (run, check, drill, ...) failed. Anything else is
reported through ubuntu-pc's `tg`, including a host that is down entirely. A snapshot made by hand also counts as
"newest snapshot" (restic's tags are encrypted), which is why the run status is checked as well. Add a host to `watch`
after its first backup (Secrets, step 4). On the host itself: `homelab-backup status`, `systemctl list-timers 'homelab-backup*'` (`systemctl --user` on ubuntu-pc).

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

The module installs restic v0.19.1 and rclone v1.76.0 (static builds pinned in `policy.toml`, sha256 checked; not
the distribution's restic, which can be older than the 0.17 the script needs) to `/usr/local/bin` or
`~/.local/bin`, `homelab-backup` + `config.json`, and enables `homelab-backup.timer`, `homelab-backup-check.timer`,
`homelab-backup-drill.timer` (and `homelab-backup-tasks.timer` where `[tasks_offsite]` is set,
`homelab-backup-watch.timer` where `watch` lists hosts). A second `--commit` changes nothing (`tests/module.sh`).

## Secrets (one time per host, by the owner)

Neither the restic password nor the Google token is ever in git. Per host:

| | system hosts | ubuntu-pc |
|---|---|---|
| restic password | `/etc/homelab-backup/restic-password` (root, 0600) | `~/.config/homelab-backup/restic-password` (0600) |
| rclone config with the `[gdrive]` remote | `/etc/homelab-backup/rclone.conf` (root, 0600) | `~/.config/rclone/rclone.conf` (existing) |

> **Keep every host's restic password offline as well** (password manager and paper). The repository is encrypted
> with it: if the host dies and the password was only on that host, its backups cannot be read by anyone.

**Why one Google Cloud project per host.** A `drive.file` token reaches the files its *app* created, and the app
is the Cloud project, not the token: two hosts authorized through the same OAuth client share every file either of
them wrote, so either token could `rclone purge` the other's repository. With one project per host, each token sees
only its own `homelab-backup-<host>` folder (and nothing else of the Drive). That is also why the folders have no
shared parent: each project would create its own copy of it. ubuntu-pc is the exception: its token has the full
`drive` scope (its repository was started under rclone's shared client, which a `drive.file` token of a new project
cannot see), so it reaches every host's folder; it is the owner's own workstation, and the watch needs that view.
From ubuntu-pc, use another host's repository read-only and without locks (`restic --no-lock snapshots|ls|restore`;
no `check`, which locks): `drive.file` access is per file, so anything ubuntu-pc's token writes there (lock files,
`forget`, `prune`, `repair`, a backup) is invisible to that host's own token, whose next prune or `check` then breaks.

1. **A Google Cloud project for the host** (in a browser, once per host): Google Cloud Console → new project
   `homelab-backup-<host>` → enable "Google Drive API" → OAuth consent screen (External, add invntrm@gmail.com as
   test user, then "Publish app": `drive.file` is a non-sensitive scope, so publishing needs no Google review, and
   a published app's token does not expire after 7 days) → Credentials → OAuth client ID, type "Desktop app". Keep
   the client id and secret in the password manager (https://rclone.org/drive/#making-your-own-client-id). The
   existing project `homelab-rclone-515215` (client in the PC's keychain, `agent/google-oauth-rclone-homelab-*`) is
   ubuntu-pc's: never reuse it on another host.
2. **On the host** (headless), the remote with that project's client and scope `drive.file`. Interactive, so neither
   the client secret nor the token lands in shell history, `ps` or sudo's log:

   ```sh
   sudo install -d -m 700 /etc/homelab-backup
   sudo rclone config --config /etc/homelab-backup/rclone.conf
   #   n (new remote) → name: gdrive → storage: drive → client_id / client_secret of this host's project
   #   → scope: drive.file → service_account_file: empty → advanced config: n
   #   → "Use web browser to automatically authenticate?": n  → it prints `rclone authorize "drive" "<blob>"`:
   #     run that on ubuntu-pc (opens the browser, prints a token) and paste the token at the prompt
   #   → shared drive: n → keep the remote: y → q
   sudo chmod 600 /etc/homelab-backup/rclone.conf
   sudo rclone about gdrive: --config /etc/homelab-backup/rclone.conf          # must print the 5 TiB quota
   sudo rclone lsd gdrive: --config /etc/homelab-backup/rclone.conf            # must list nothing but this host's folder
   ```

3. **Restic password**, then the repository. `init` is the only command that creates a repository: a nightly run
   that finds none fails and says so, rather than silently starting a new history.

   ```sh
   sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 > /etc/homelab-backup/restic-password'
   sudo cat /etc/homelab-backup/restic-password                               # → password manager + paper
   sudo homelab-backup init        # NixOS: in PATH; Debian: /usr/local/lib/homelab-backup/homelab-backup init
   ```

   **Reinstalling a host that already has a repository** (odroidn2: Armbian → NixOS): do not generate a new
   password and do not create a new project. Put the existing password from the password manager into
   `/etc/homelab-backup/restic-password`, authorize step 2 with the host's existing project (a new project cannot
   see the old folder), check with `sudo homelab-backup snapshots`, and skip `init`. A wrong password fails every
   run with "wrong restic password". A token from another project fails it with "no repository visible ... do not
   init": running `init` then would start a second `homelab-backup-<host>` folder and split the history (the watch
   reports two folders of that name).

4. **Watch it from ubuntu-pc**: after the host's first nightly backup, add it to `watch` in `hosts/ubuntu-pc.toml`
   and run `machines/backup/debian/install.sh ubuntu-pc --commit` on the PC.

ubuntu-pc: the `[gdrive]` remote uses the client of project `homelab-rclone-515215` with scope `drive` (see above).
Its password was created once with `umask 077; head -c 32 /dev/urandom | base64 > ~/.config/homelab-backup/restic-password`:
copy it offline.

## Restore

Every command below runs as the backup user (root on system hosts) with the host's settings:
`homelab-backup restic ...` is plain restic with the repository, password and rclone config already set.

```sh
homelab-backup snapshots                                   # list this host's snapshots
homelab-backup restic ls latest --tag nightly /home/ultra/homeassistant  # browse
# restore into a scratch dir first, never straight over live data; `--tag nightly` skips snapshots made by hand
homelab-backup restic restore latest --tag nightly --target /var/tmp/restore --include /home/ultra/homeassistant/.storage --verify
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

A whole host from a machine that has none of its files, given the password and an rclone config whose token can see
the folder (the host's own project, or ubuntu-pc's full-scope `[gdrive]` with `--no-lock`, see Secrets):

```sh
export RESTIC_REPOSITORY=rclone:gdrive:homelab-backup-odroidn2/restic RESTIC_PASSWORD_FILE=./restic-password
export RCLONE_CONFIG=./rclone.conf
restic --no-lock snapshots
restic --no-lock restore latest --host odroidn2 --tag nightly --target /mnt/newroot --verify
```

Tasks sets: `rclone copy gdrive:homelab-backup-ubuntu-pc/tasks/<RUNID> ./<RUNID>`, then the Tasks restore procedure
(`python3 -m tasks_backup.drill` decrypts them with `~/.config/tasks-backup/age.key`, itself in the ubuntu-pc
repository).

## Test

```sh
machines/backup/tests/roundtrip.sh   # the script against a local restic repo in /var/tmp; exit 0 = all checks passed
machines/backup/tests/module.sh      # debian/ (scope = "user") converges and stays put, in a scratch $HOME
machines/backup/tests/in-docker.sh   # both of the above as root in a Debian trixie container (scope = "system")
shellcheck -x machines/backup/lib/homelab-backup machines/backup/debian/run machines/backup/debian/install.sh machines/backup/tests/*.sh
nix flake check --no-build ./machines
```

The round trip covers: `run` refusing a missing repository and `init` creating it once, dumps of live WAL databases
(a row that is only in the WAL is in the dump), the snapshot, excludes, a full restore with an empty `diff -r` and
identical SQL content, the drill (past a snapshot made by hand), a second run that adds no data, retention on
backdated snapshots, `check`, a failing dump and a too-full state disk (status + notify, the previous dump kept, no
work dir left), one run or dump at a time, a missing password file, the status copies in the Drive folder, the watch
of a healthy, a stale, a failing and a missing host, the Tasks copy with the puller's retention against a local rclone remote,
and, with docker, Postgres and Redis dumps from live containers restored into fresh ones. As root (`in-docker.sh`)
the databases belong to another user, and must stay writable for it after the dump.
