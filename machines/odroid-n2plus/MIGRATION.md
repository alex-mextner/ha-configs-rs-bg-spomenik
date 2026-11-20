# Moving odroidn2 from Armbian to NixOS

The board keeps its name (`odroidn2`), its address (192.168.0.39), its SSH host keys, its tailnet node and its
disks. Only the eMMC module changes: the Armbian module (116 GB) comes out and stays untouched as the fallback, the
NixOS module (64 GB, flashed with `scripts/build-sd-image.sh`) goes in. Every step says how to go back.

Data model after the move: everything the services need lives on the eMMC. `/home/ultra` and the containers'
data are copied from the WD500 (where Armbian ran them, `/mnt/wd500/odroid-rt/`) onto the eMMC; Docker starts
fresh on the eMMC with the images carried over. The WD500 only keeps the staging copy and the torrents (media-only
later, ticket #110), and `odroid-rt/` stays exactly as Armbian left it, which is what makes the rollback a swap of
modules. NixOS reads `odroid-rt/` and never writes to it (the one exception is the DHCP lease file in Rollback).

Roles: **owner** = Alex at the board (eMMC swap, power); **operator** = whoever runs the commands (owner or an agent
with `ssh root@odroidn2` on Armbian and `ssh ultra@192.168.0.39` on NixOS). Commands run on the board unless they
say "on the PC".

Downtime: about 1.5-2 hours for Home Assistant, the LAN's DNS relay (only the `living` devices use it; all other
clients get the ISP's DNS servers from DHCP), Kodi, mextner.com, and Anton's SFTP.

## 0. Gate: before the day

All of these must hold; otherwise do not start.

- **The bots already run on bananapi-m4.** HyperCalendarBot (+ redis, stage QA), ExpenseSyncBot (+ stage, bank
  sync), HyperSummaryBot, their five CI runners (`odroid`, `odroid-esb-qa`, `odroid-hcb`, `odroid-hcb-qa`,
  `odroid-hsb`), their root/www-data cron jobs (DB backups 03:00, log retention, ESB health alert) and the
  `finbot.mextner.com`, `finbot-stage.mextner.com`, `hypercal.mextner.com` (and HyperSummaryBot's) routes in the
  Cloudflare dashboard point at bananapi-m4, and the bots' units on Armbian are stopped.
  Why not move them with this cutover: they are not part of the NixOS host (`README.md`), so they would have to
  run here either as a throwaway Docker port of three pm2/bun apps plus six runners that deploy to Armbian paths,
  or stay down for days until bananapi-m4 is up. Both are worse than keeping them on Armbian until bananapi-m4
  takes them; and the ~0.7 GB they use (plus ~1 GB of swap) is exactly what makes 3.7 GB enough for the rest.
- The PR with this file is merged on `main` (the board rebuilds from GitHub in step 8).
- The NixOS eMMC module is flashed with the image of `README.md` ("Build the SD/eMMC image").
- On the PC: the admin age key in `~/.config/sops/age/keys.txt`, `gh` logged in (runner token, step 8).
- The restic password of `homelab-backup-odroidn2` is in the password manager (step 1 and 11).
- **Hard rule: the window does not overlap Anton's backup windows.** His Nextcloud backs up to this SFTP
  (127.0.0.1:2224 behind his tailnet), and the service must be up while it runs. The windows are not observable
  from ultra's side: rclone logs only NOTICE to his 0750 home, his tailscaled runs in userspace (no sshd or
  kernel trace), and the journal holds only his cron lines. So ask Anton for his backup schedule, write it here,
  and choose the window outside it with at least an hour of margin on both sides:
  - Anton's backup windows: _not yet confirmed; this step stays open until they are listed here_
  Then tell him the date and that his SFTP is down for ~2 hours in the window.

Timing: a 2-3 hour daytime window when nobody needs the projector, the owner at the board for steps 2 (optional), 5
and a possible rollback.

## 1. The day before: longer DHCP leases

dnsmasq hands out 1-hour leases (`dhcp-range=...,3600s`): clients renew every 30 minutes, so a DHCP outage longer
than ~30 minutes starts dropping addresses. Raise it to 12 hours at least an hour before the window, so every client
has renewed with the long lease:

```sh
# Armbian, as ultra
cd ~/.local/share/home-lan
cp dhcp/dnsmasq-dhcp.conf dhcp/dnsmasq-dhcp.conf.bak-nixos-move
sed -i 's/^dhcp-range=192.168.0.10,192.168.0.254,255.255.255.0,3600s$/dhcp-range=192.168.0.10,192.168.0.254,255.255.255.0,12h/' dhcp/dnsmasq-dhcp.conf
# the sed changes nothing if the line differs: restart only once the 12 h lease is really in place
grep -q '^dhcp-range=.*,12h$' dhcp/dnsmasq-dhcp.conf \
  && docker exec home-lan-dhcp dnsmasq --test --conf-file=/etc/dnsmasq-dhcp.conf && ./dc restart dhcp
```

Rollback: restore the `.bak-nixos-move` file and `./dc restart dhcp`. Step 12 sets 3600s again on NixOS.

## 2. Optional dry run: does the NixOS module boot?

De-risks the swap for ~15 minutes of downtime. Owner: `sudo poweroff` on Armbian, swap the eMMC modules, power on.
Operator: `ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ultra@192.168.0.39 'uname -a; df -h /'`
must answer (the fresh image has its own host key). Owner: power off, swap back, power on; Armbian comes back by
itself (nothing was changed on it).

## 3. Final backup to Google Drive

On Armbian, with `homelab-backup` installed (`machines/backup/README.md`, Install: `debian/install.sh odroidn2`):

```sh
sudo homelab-backup run; sudo homelab-backup status   # status-run.json: ok, or only the bots' dumps failed
sudo homelab-backup snapshots | tail -3                   # a snapshot from now
# odroidn2.toml still lists the bots' dumps (hypercal-redis ...): with the bots moved (step 0) those dumps fail and
# the run reports failure, but the snapshot is written. Any other failed dump: stop and fix it first.
```

If it was never installed there, install it per that README (Secrets steps 1-3: the host's own Google Cloud
project, a new restic password into the password manager, `homelab-backup init`) and run it. NixOS reuses the same
project, password and repository in step 11. Rollback: nothing to undo.

## 4. Stop services on Armbian, stage what lives on its eMMC

As root on Armbian (`ssh root@odroidn2`). Only `stop`, never `disable`: a reboot of Armbian brings everything back.

```sh
# pre-flight, right before anything stops: no open SFTP session of Anton (his tailscaled -> rclone on :2224).
# Any line printed means a transfer is running: stop here and wait for it to finish (re-run until it is empty).
ss -Htn state established '( sport = :2224 )'

# ultra's user units, then cron (ultra's HA fail-safe checks, Anton's SFTP watchdog; the bots' jobs are gone)
sudo -u ultra XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop ha-mdns.service printops-backup.timer \
  router-discover.timer router-inventory-update.timer router-scan.timer tasks-backup-pull.timer tasks-backup-drill.timer
systemctl stop cron
systemctl stop kodi ha-host-actions ha-soil-moisture transmission-daemon glances caddy-mextner cloudflared \
  actions.runner.alex-mextner-mextner.com.odroid-mextner
systemctl stop ha-compose                   # compose stop: HA flushes its recorder
docker stop printops-api printops-spoolman
pkill -u anton -f 'rclone serve sftp'; pkill -u anton -x tailscaled

# images of the containers that move (local builds included: ha-docker-dns, home-lan-*, printops/3d-api);
# avahi-reflector is pinned by digest and is pulled again on NixOS
install -d -m 700 /mnt/wd500/nixos-migration
docker ps -a --format '{{.Image}}' | sort -u | grep -v -e autoheal -e '^[0-9a-f]\{12\}$' \
  | xargs docker save -o /mnt/wd500/nixos-migration/images.tar

# last: DHCP, DNS relay, TLS front (from here the LAN has no DHCP server; leases are 12 h, step 1)
docker stop home-lan-dhcp home-lan-dns-relay home-lan-tls-voice

# what lives on Armbian's eMMC and must move (secrets included: the staging dir is root-only)
S=/mnt/wd500/nixos-migration/armbian
install -d -m 700 "$S"
rsync -aHAX --numeric-ids --relative /etc /var/lib/tailscale /var/lib/bluetooth /var/lib/alsa \
  /home/anton /var/www/mextner.com /var/www/mextner.com.caddy /var/spool/cron/crontabs "$S/"
ls -la "$S/etc/ssh/ssh_host_ed25519_key" "$S/home/anton/.local/state/ts-owner/tailscaled.state" \
  "$S/var/lib/tailscale/tailscaled.state"

# size of what step 7 copies onto the 64 GB eMMC (nothing runs now, so the disk is idle)
du -xsh /mnt/wd500/odroid-rt/ultra
du -xh --max-depth=1 /mnt/wd500/odroid-rt/ultra | sort -h | tail -25
```

Go on only if `/mnt/wd500/odroid-rt/ultra` minus `.cache`, `.npm` and `Downloads` is under 30 GB (the eMMC keeps
>25 GB free for Docker images, the Nix store and growth). Otherwise stop here, roll back, and decide which big
directories go to the WD500 instead.

The live HA checkout (`~/homeassistant`) carries an unpushed commit and uncommitted changes; they move with the
copy. Note `git -C ~/homeassistant log -1 --oneline` and `git -C ~/homeassistant status --short | wc -l` to compare
in step 10.

Rollback: `reboot` (every stopped unit and container is enabled or restart-policy `unless-stopped`; cron restarts
Anton's SFTP within 2 minutes).

## 5. Swap the eMMC (owner)

```sh
poweroff                                     # Armbian, as root
```

Owner: unplug power, take the Armbian eMMC module out (label it "Armbian, fallback"), put the NixOS module in. Leave
both USB disks on their ports. Power on.

Rollback: power off, swap back, power on.

## 6. First boot: host identity and state into place

The fresh image answers on 192.168.0.39 with its own host key, and the LAN has no DHCP server yet. From the PC:

```sh
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i ~/.ssh/id_ed25519_home ultra@192.168.0.39
```

On the board:

```sh
id ultra                                     # uid=1000 (the first normal user of the image)
# the image already carries storage.nix, so /mnt/wd500 is normally mounted at boot
mountpoint -q /mnt/wd500 || sudo mount -o noatime /dev/disk/by-uuid/83c5bd3c-2cfa-494e-b0a5-17d3c92d0925 /mnt/wd500
RSYNC=$(nix build --no-link --print-out-paths github:NixOS/nixpkgs/f68407657ad294471eb4b2e65d193df26edf729f#rsync)/bin/rsync
S=/mnt/wd500/nixos-migration/armbian

# SSH host keys: known_hosts stays valid, and the ed25519 key is the sops recipient `odroidn2` (secrets/)
for k in ssh_host_ed25519_key ssh_host_rsa_key; do
  sudo install -m 600 "$S/etc/ssh/$k" /etc/ssh/$k
  sudo install -m 644 "$S/etc/ssh/$k.pub" /etc/ssh/$k.pub
done
sudo systemctl restart sshd   # from here the PC's known_hosts entry for the board matches again

# ultra's tailnet node (same name and tailnet address), Bluetooth pairings, HDMI audio mixer state
sudo systemctl stop tailscaled
sudo $RSYNC -aHAX --numeric-ids --delete "$S/var/lib/tailscale/" /var/lib/tailscale/
sudo $RSYNC -aHAX --numeric-ids "$S/var/lib/bluetooth/" /var/lib/bluetooth/
sudo install -D -m 644 "$S/var/lib/alsa/asound.state" /var/lib/alsa/asound.state

# Anton's home as it is (uid 1002; the image already has the user with that uid). His units run from first boot:
# stop them so his tailscaled never starts on a half-copied state dir, then start them on the copied state.
sudo systemctl stop anton-storage-sftp.timer anton-storage-sftp tailscaled-anton
sudo $RSYNC -aHAX --numeric-ids "$S/home/anton/" /home/anton/
sudo systemctl start tailscaled-anton anton-storage-sftp.timer

# mextner.com (served by caddy-mextner, deployed by its runner; owner fixed in step 9)
sudo mkdir -p /var/www
sudo $RSYNC -aHAX --numeric-ids "$S/var/www/mextner.com" "$S/var/www/mextner.com.caddy" /var/www/
```

Do not touch `/mnt/storage` (Anton's disk): `anton-storage-sftp.timer` mounts it (automount) and serves it.

Rollback: power off, swap back the Armbian module, power on.

## 7. ultra's home and the Docker images onto the eMMC

```sh
sudo $RSYNC -aHAX --numeric-ids --info=progress2 \
  --exclude=/.cache/ --exclude=/.npm/ --exclude=/Downloads/ --exclude=/.local/share/Trash/ \
  /mnt/wd500/odroid-rt/ultra/ /home/ultra/
sudo mkdir -p /home/ultra/Downloads /mnt/wd500/torrents
# the torrent data (Transmission's resume files and Kodi's library point at ~/Downloads, which becomes a bind of
# /mnt/wd500/torrents): hardlinks on the same disk, instant, and odroid-rt stays as it was
[ -d /mnt/wd500/odroid-rt/ultra/Downloads ] && sudo cp -al /mnt/wd500/odroid-rt/ultra/Downloads/. /mnt/wd500/torrents/
# the user units are system units now (modules/ultra-timers.nix, ha-mdns in home-assistant.nix)
rm -f /home/ultra/.config/systemd/user/default.target.wants/ha-mdns.service \
  /home/ultra/.config/systemd/user/timers.target.wants/*.timer
sudo docker load -i /mnt/wd500/nixos-migration/images.tar
sudo docker image ls
```

`/home/ultra` holds everything the stacks need: `homeassistant` (config, `.storage`, `home-assistant_v2.db`,
`zigbee.db`, `secrets.yaml`, `.env`), `hga-postgres` (uid 999), `music-assistant`, `esphome`, `ssh-data`,
`odroid-overlay/go2rtc.yaml`, `.local/share/home-lan` + `.local/state/home-lan` (dnsmasq config and leases),
`printops`, `.kodi` + `KodiLibrary`, `.config/transmission-daemon` and `.config/ha-host-actions`, `.local/bin`,
`.local/lib/tasks-backup`, `dev/router-cli`, `.env`.

Rollback: power off, swap back, power on (the copy only read from `odroid-rt/`).

## 8. Switch to the full configuration

The runner registration token is valid for one hour: create it right before the switch. On the PC:

```sh
gh api -X POST repos/alex-mextner/mextner.com/actions/runners/registration-token --jq .token \
  | ssh ultra@192.168.0.39 \
    'sudo install -d -m 700 /var/lib/github-runner-tokens && sudo install -m 600 /dev/stdin /var/lib/github-runner-tokens/odroid-mextner'
```

On the board (it is idle, so it may evaluate and download on its own; ~15-30 minutes):

```sh
sudo nixos-rebuild switch --refresh \
  --flake 'github:alex-mextner/ha-configs-rs-bg-spomenik?dir=machines#odroid-n2plus'
```

This creates `anton` (1002) and `mextner-site`, mounts the WD500, decrypts `cloudflared-token` with the restored
host key, and starts everything: `home-lan` first matters most (DHCP and DNS are back the moment it is up).

Rollback: power off, swap back, power on (see Rollback for the DHCP leases given out meanwhile).

## 9. Fix-ups and one reboot

```sh
sudo chown -R mextner-site:mextner-site /var/www/mextner.com /var/www/mextner.com.caddy
sudo systemctl restart caddy-mextner
sudo reboot                                  # proves that everything comes up from a cold start
```

From now on the board has its Armbian host key again: plain `ssh ultra@odroidn2` works with the old known_hosts.

## 10. Verify

```sh
systemctl --failed                                         # nothing
systemctl status ha-compose home-lan printops ha-host-actions ha-mdns ha-soil-moisture cloudflared \
  caddy-mextner transmission glances tailscaled tailscaled-anton anton-storage-sftp github-runner-odroid-mextner
docker ps --format 'table {{.Names}}\t{{.Status}}'          # all up, healthchecks healthy
free -m; cat /proc/pressure/memory; systemctl show docker.slice -p MemoryCurrent,MemoryMax
git -C ~/homeassistant log -1 --oneline; git -C ~/homeassistant status --short | wc -l   # as noted in step 4
```

| Check | How | Expected |
|---|---|---|
| Home Assistant | http://192.168.0.39:8123 and the app; Settings → System → Repairs; Logbook | loads, history continues from before the stop, no new repairs |
| HA update notice | Settings → System → Updates; `systemctl status ha-core-update-notice ha-core-update-notice.timer`; `python3 ~/homeassistant/scripts/provision_ha_core_update.py --check` after the checkout contains this script | one available Home Assistant Core notice, current running/latest versions and release link, all four reminder steps; restored entries reused, no install action |
| DHCP | on a test client: forget and rejoin Wi-Fi (or `sudo dhclient -r && sudo dhclient`); `docker logs --since 10m home-lan-dhcp` | DHCPACK from 192.168.0.39, the client's pinned address |
| DNS | `dig @192.168.0.39 ya.ru +short`; a `living` device (Yandex speaker) answers a voice request | answers |
| IR | `ls -l /dev/lirc0`; press a remote button, HA Developer tools → Events of `ekf_appliances` | `crw-rw---- root video`, the event arrives |
| Soil ADC | `curl -s http://127.0.0.1:8768/reading/fresh`; HA `sensor.strelitzia_soil_adc` | a reading; the sensor updates |
| Kodi on HDMI | switch the projector on from HA; `systemctl status kodi`; `curl -s -u kodi:<password> http://127.0.0.1:8080/jsonrpc -d '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.Ping"}' -H 'content-type: application/json'` | picture and sound on the projector, `pong` |
| Cloudflare Tunnel | `journalctl -u cloudflared -b \| grep -c 'Registered tunnel connection'`; `curl -sI https://mextner.com` | 4; HTTP 200; the connector `odroidn2` is healthy in the dashboard |
| mextner.com origin | `curl -s -o /dev/null -w '%{http_code}' -H 'Host: mextner.com' http://127.0.0.1:8081/` | 200 |
| Deploy runner | GitHub → mextner.com → Settings → Actions → Runners; re-run the last `deploy` workflow | `odroid-mextner` idle; the run publishes |
| Tailscale | `tailscale status`; `tailscale serve status`; `curl -kI https://odroidn2.<tailnet>.ts.net` from the PC | `odroidn2` with its Armbian tailnet address, the same serve config |
| Anton | `ss -tlnp \| grep 2224`; `sudo -u anton tailscale --socket=/home/anton/.local/state/ts-owner/tailscaled.sock status`; `tail /home/anton/.config/storage-sftp/sftp.log`; Anton checks his Nextcloud external storage | listening; his node online; he can list and write |
| Bluetooth | HA → Bluetooth integration | adapter present, devices seen |
| Music Assistant, ESPHome | http://192.168.0.39:8095, http://192.168.0.39:6052 | load, players visible |
| mDNS/HomeKit | `homeassistant.local` from the PC; Home app accessories | resolves; accessories respond |
| Transmission, Glances, PrintOps | `curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:9091/transmission/rpc`; `curl -s 127.0.0.1:61208/api/4/cpu`; `curl -s 127.0.0.1:7912/api/v1/health` | 409; JSON; `{"status":"healthy"}` |

## 11. Backups on NixOS

`machines/backup/README.md`, Secrets, "Reinstalling a host that already has a repository": the existing restic
password into `/etc/homelab-backup/restic-password`, `rclone config` with the host's existing Google Cloud project
into `/etc/homelab-backup/rclone.conf`, then:

```sh
sudo homelab-backup snapshots                # the snapshot of step 3 is there; never `init`
sudo systemctl start homelab-backup && sudo homelab-backup status
```

## 12. Afterwards

- Same day, when all checks pass: 1-hour leases again, on NixOS: in `~/.local/share/home-lan/dhcp/dnsmasq-dhcp.conf`
  replace `,12h` by `,3600s` in `dhcp-range`, `docker exec home-lan-dhcp dnsmasq --test --conf-file=/etc/dnsmasq-dhcp.conf`,
  `~/.local/share/home-lan/dc restart dhcp`.
- Same day: the plant sensor recovery (`scripts/host/ha_plant_sensor_recovery.py` on the controller) restarts the
  soil bridge with `ssh root@odroidn2`, which NixOS refuses (`PermitRootLogin = "no"`). Switch it to
  `sudo -n systemctl` as the login user (ultra is passwordless wheel here) and redeploy it on the controller.
- After a week without rollback: delete the staged secrets and images,
  `sudo rm -rf /mnt/wd500/nixos-migration`. `odroid-rt/` and the Armbian module stay as the fallback until the owner
  retires it (ticket #110 frees the WD500).

## Rollback (any step from 5 on)

1. Optional, to keep the DHCP leases NixOS gave out (otherwise Armbian's dnsmasq may offer an address that a
   client got from NixOS; dnsmasq pings before offering, so a clash is unlikely and lasts one lease):
   `sudo cp /home/ultra/.local/state/home-lan/dhcp/dnsmasq.leases /mnt/wd500/odroid-rt/ultra/.local/state/home-lan/dhcp/dnsmasq.leases`
2. `sudo poweroff` on NixOS; owner swaps the Armbian module back in; power on. Armbian starts every service at boot
   from `odroid-rt/` as it was at step 4.
3. What is lost: Home Assistant history and anything changed in the apps between step 4 and the rollback. To keep
   HA's state instead, copy `/home/ultra/homeassistant` back over `odroid-rt/ultra/homeassistant` before the
   poweroff (only if HA ran fine on NixOS).
4. If step 8 ran: the mextner.com runner on Armbian was replaced by the NixOS registration. Re-register it on
   Armbian (`/var/www/actions-runner-mextner`: `./config.sh remove`, then `./config.sh --url
   https://github.com/alex-mextner/mextner.com --token <new token> --name odroid-mextner --labels odroid --replace`
   as www-data) or leave deploys waiting until the next attempt.
5. If step 1 ran: put the 3600s leases back on Armbian (step 1, Rollback).
