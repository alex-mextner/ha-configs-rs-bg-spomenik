# Kodi startup recovery on the ODROID-N2

## Observed failure and scope

On 16 November 2026 the on-demand Kodi service failed before its GUI with
`DRM_IOCTL_MODE_CREATE_DUMB: Cannot allocate memory`, followed by exit 139.
The host had roughly 0.8 GiB of generally available memory but only 2–10 MiB
reported free in its 256 MiB contiguous memory area (CMA). Free CMA alone does
not establish a leak or prove that all pages in the area are immovable.

One normal compaction attempt did not increase free CMA. One clean page-cache
reclaim did increase it, but doing that before the projector's HDMI connection
left about 25 seconds for the reserve to refill. A single reclaim after the
existing HDMI-ready condition, immediately before the unchanged Kodi session,
allowed the GUI and real 1080p H.264/AAC playback to start. The captured CMA trace
recorded four successful allocations of 2089 pages and no allocation failure.
The temporary experiment was removed; its effective base unit was restored.

This helper retains that bounded recovery at the next normal Kodi service start.
It is a workaround for the measured allocation failure, not a claim to repair
a kernel or driver leak.

## Admission and limits

The root-owned helper accepts only the ODROID-N2/N2Plus device-tree identity.
It waits at most 40 seconds for the same connected HDMI status used by the
existing `kodi-session`. It does not turn on the projector or start Kodi.
An unsupported board or a disconnected display skips recovery, leaving the
existing session's behavior in force.

If free CMA is below 96 MiB, it runs `sync` from the unit's PATH with a requested
ten-second subprocess timeout, rechecks HDMI and free CMA, then writes **1** to
`/proc/sys/vm/drop_caches` at most once. That operation drops clean page-cache
entries; it does not request slab-cache eviction. There is no periodic job,
retry loop, automatic service restart, termination of unrelated processes or
kernel tuning.
Recovery errors are logged. This is an optional pre-start command: its `-`
prefix lets the unchanged Kodi main command run even if recovery fails or times
out. Continuing startup does not declare recovery or playback successful; the
existing HA readiness and native playback checks still decide that. A low
measurement after the single attempt is recorded, not retried. The verified
existing service has `Restart=no`; this drop-in adds no restart policy.

The threshold is the same admission bound used in the successful observation.
It is deliberately above the approximately 33 MiB of observed GUI allocations.
It is not a guaranteed allocation size and does not by itself prove playback.

The [Linux 6.18 documentation](https://kernel.org/doc/html/v6.18/admin-guide/sysctl/vm.html#drop-caches)
warns that cache eviction can impose system-wide I/O and CPU costs and should
not be used as routine cache management. This deployment therefore scopes it
to the observed board, a requested connected-display startup and low CMA.
Each invocation records the decision and before/after measurements in a private
`/tmp/ha-kodi-prestart-*.log`, as well as stderr/journald.

## Installation and rollback

After applicable review and native CI, install
`scripts/host/ha_kodi_prestart.py` as root-owned mode 0755 at
`/usr/local/libexec/ha_kodi_prestart.py`. Install
`scripts/host/kodi-memory-prestart.conf` as root-owned mode 0644 at
`/etc/systemd/system/kodi.service.d/60-ha-cma-prestart.conf`.
Back up any existing target and verify exact source and destination hashes.

The drop-in uses the documented
[systemd 257 command privilege prefix](https://www.freedesktop.org/software/systemd/man/257/systemd.service.html#ExecStart=)
only for this pre-start helper. Python runs in isolated mode so it cannot import
a user's module or site customization as root. The inner sync timeout is not an
absolute ten-second wall-clock bound: Python waits for the killed child, and
kernel writeback can delay signal handling. The outer timeout requests helper
termination after 55 seconds and SIGKILL after a further two seconds. These
signal deadlines do not guarantee when an uninterruptible kernel operation
returns. The `-` prefix ignores a failed helper exit; the `+` prefix grants root
only to that command. The main Kodi command, its user, resource limits and
existing I/O-priority drop-in remain unchanged.

Run `systemctl daemon-reload` to load this scoped drop-in. Installation does not
start or restart Kodi. The next authorized normal HA command exercises it.
A running or unrelated user's media session must be preserved.

Rollback removes only this owned drop-in and restores its saved predecessor,
if any; run `systemctl daemon-reload` again. Remove the helper only if no current
unit references it. Do not stop an already running Kodi session just to roll back.

## Verification

The native Kodi fireplace workflow discovers `test_kodi_prestart.py` with the
host suite. Tests use mocked kernel interfaces and subprocesses; they never
write a runner's real memory controls. They cover the board, privilege, HDMI
and CMA guards, no cache drop after a reported sync failure, disconnect during
sync, memory freed during sync and the one-attempt bound.

Real acceptance must additionally show a successful normal service start,
actual media identity, advancing playback and the expected HDMI audio route.
The HA script allows at most 120 seconds for the cold physical/native readiness
phase, exiting as soon as the existing readiness proof is true. The observed
successful process was still initializing 70 seconds after the original command,
which exceeded the former 45-second bound. A newer power intent still cancels
that wait; extraction, playback and stop deadlines are unchanged.

Startup recovery alone does not satisfy the fireplace rotation or receipt
criteria. All application tests and builds run in CI or managed Runpod.
