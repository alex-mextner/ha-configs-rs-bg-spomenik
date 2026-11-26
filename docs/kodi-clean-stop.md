# Kodi stops cleanly under systemd (Armbian host)

`kodi.service` is on demand: HA's host bridge runs `sudo -n systemctl stop kodi.service` when the
projector is switched off. Before this change every such stop ended the unit as
`failed (Result: exit-code)`, main status 143, so service-failover raised a repair session after
each movie night, and Kodi was sometimes SIGKILLed while still saving its state (#61).

## Cause

`/usr/local/bin/kodi-session` ended with `exec /usr/bin/kodi`, Debian's shell wrapper. The wrapper
starts `kodi.bin` in the background and runs `trap 'kill -TERM $CHILD' TERM; wait $CHILD; exit $?`.
A trapped signal makes POSIX `wait` return 128+15 at once, so on SIGTERM the wrapper (the unit's
main process) exits 143 immediately while `kodi.bin` keeps shutting down for 18-30 s. systemd sees
a main process that died with a non-zero status and marks the unit failed.

## Fix

- `scripts/host/kodi-session` runs `kodi.bin` directly, so the main process is Kodi and SIGTERM
  ends it with exit 0. It keeps the wrapper's `MALLOC_MMAP_THRESHOLD_` and locale unset.
  Dropped with the wrapper: its crash-report text file and its restart loop on exit 65.
- `stop_kodi` in `scripts/host/ha_host_fireplace.py` waits up to 70 s for `systemctl stop`
  (was 20 s): the command now returns only after Kodi has saved its state. Deploy this bridge
  change **before** the new `kodi-session`, otherwise a slow stop raises `TimeoutExpired`.
  `packages/host_actions.yaml` raises the `host_projector_off` and `host_kodi_stop` client timeouts
  from 12 s to 100 s (JSON-RPC quit, 8 s poll, then up to 70 s of stop) for the same reason (reload HA package config after deploy).
- `kodi-session` removes a stale `/tmp/.X99-lock` (only when no Xvfb of this user runs) before starting Xvfb, because `KillMode=mixed`
  SIGKILLs Xvfb and leaves it behind.
- `scripts/host/kodi-clean-stop.conf` adds `TimeoutStopSec=60` (Kodi needs up to 30 s),
  `KillMode=mixed` (Xvfb is killed only after Kodi exited) and `RestartForceExitStatus=65`
  (Kodi's "restart application" code, formerly looped by the wrapper).

## Install (root)

Install the updated bridge (`scripts/host/ha-host-actions.py`, `ha_host_fireplace.py`) and restart
`ha-host-actions.service` first, then:

```sh
install -o root -g root -m 0755 scripts/host/kodi-session /usr/local/bin/kodi-session
install -o root -g root -m 0644 scripts/host/kodi-clean-stop.conf \
  /etc/systemd/system/kodi.service.d/60-clean-stop.conf
systemctl daemon-reload
systemctl reset-failed kodi.service
```

Back up the previous `/usr/local/bin/kodi-session` first. Install only while `kodi.service` is
inactive or failed (`systemctl is-active kodi.service`): `daemon-reload` applies `KillMode=mixed` to
a running instance at once, and the old wrapper (still its main process) exits 143 on SIGTERM, which
under `mixed` would SIGKILL Kodi mid-save. Never stop a running session just to install.

## Verify

After the next start and projector-off:

```sh
systemctl show kodi.service -p ActiveState,Result,TimeoutStopUSec,KillMode
journalctl -u kodi.service -n 20   # 'Deactivated successfully', no 'status=143'
```

Expected: `ActiveState=inactive`, `Result=success`, `TimeoutStopUSec=1min`, `KillMode=mixed`.

## Rollback

Remove `60-clean-stop.conf`, restore the saved `kodi-session`, `systemctl daemon-reload`.
