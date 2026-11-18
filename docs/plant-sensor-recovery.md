# Plant sensor recovery

The care integration marks a stagnant or invalid measurement as unreliable. The
host controller responds to that persisted incident, without changing calibration
or making watering decisions.

## Recovery sequence

The Ubuntu user timer checks once a minute. Home Assistant credentials remain on
Odroid; the controller receives only the four relevant entities and bridge health.

1. Request a fresh physical ADC reading and refresh the HA source.
2. Wait at least five minutes after that request completes, then repeat the
   physical reading and check the care state. A transport failure remains pending
   and does not consume a confirmed reading.
3. If both physical requests failed, request a restart of only
   `ha-soil-moisture.service` once, then obtain another sample after five minutes.
   An uncertain SSH reply does not justify a duplicate restart.
4. If the integration still reports the incident after verification, start one
   saved OMP repair session, at least ten minutes after first observation. A healthy
   ADC beyond the wet calibration point is recorded as saturation and does not
   cause a service restart. Each confirmed measurement and failure stays in the
   incident action history even when later observations replace its current state.

The integration alone confirms sustained recovery. An unavailable entity, a new
packet or the existence of an OMP process never means repaired. The controller
requires an available care state and a physical sample at most 20 minutes old
before accepting recovery. A transition into `recovering` gets a separate bounded
30-minute confirmation window. Bootstrap `starting` without a persistent incident
identity cannot launch a repair.

## Installation

Install the same `scripts/host/ha_plant_sensor_recovery.py` on:

- Odroid: `/home/ultra/homeassistant/scripts/host/ha_plant_sensor_recovery.py`.
- Ubuntu: `/home/ultra/.local/share/ha-plant-sensor-recovery/ha_plant_sensor_recovery.py`.

Install the service and timer in Ubuntu's `~/.config/systemd/user/`, reload the
user manager and enable `ha-plant-sensor-recovery.timer`. Activate only after the
care integration exposes the reliability contract below. All operations are quiet.

The existing SSH identity must reach `odroidn2`. The controller invokes
`root@odroidn2` only for the dedicated soil service restart. This limits its command
path; it does not replace the host's existing SSH permissions or OMP approval
policy. Calibration changes, physical watering, unrelated device control and
broad resets are outside this recovery procedure.

The ordinary probe has a 45-second SSH deadline. Fresh measurement allows 90
seconds for up to 76 seconds of local HTTP work plus connection overhead. The
sequential observation and fresh request fit within the monitor service's
180-second deadline. Retry reconciliation and dispatch have a smaller budget.

Expected SSH/API outages exit with status 75 and remain deferred. Missing scripts,
credentials or authorization are recorded as `blocked` in the private monitor
status, rather than silently classified as network delays. The command `status`
includes these diagnostics. `SuccessExitStatus=75` and
`RestartPreventExitStatus=75` prevent the separate generic service-failure repairer
from creating an unrelated OMP session. OMP sessions use transient units, which
the existing generic repairer already excludes.

## Reliability contract

`sensor.strelitsiia_poliv` exposes:

| Attribute | Meaning |
| --- | --- |
| `reliability_valid` | Whether care calculations can trust the measurement. |
| `reliability_reason` | Unchanged, saturated, stale, invalid, starting or recovering. |
| `reliability_incident_started_at` | Persistent UTC epoch identifying the episode. |
| `last_sample_at` | UTC epoch of the physical sample, independently of HA writes. |
| `last_value_change_at` | Last actual numeric change. |
| `stagnant_seconds` | Duration of the unchanged value. |

Missing contract fields defer action. Invalid/future incident timestamps fail
closed. The identity uses the entity and persisted start timestamp, not its current
reason. Missing supplementary calibration helpers stay visible in diagnostics but
do not disable first aid. Malformed bridge JSON counts as a failed measurement.

## Persistence and native OMP execution

Private state is under `~/.local/state/ha-plant-sensor-recovery/`. It is atomically
saved and guarded by an exclusive monitor lock. A launch claim is saved before
dispatch. The stable systemd unit, its process cgroup and the exact root session
file are reconciled before retrying. A lost reply remains unknown; confirmed
absence of the launch identities permits retry of the same incident with backoff.

A separate `Type=exec` transient service in `omp.slice` runs the installed
`~/.local/bin/omp` wrapper in native print mode with a saved session directory.
The normal authentication, model selection, admission, Landlock signal isolation
and memory guards remain active. No skip environment variables or alternative
binary are introduced. The service keeps the existing OMP memory limits, a
25-minute outer lifetime and a 20-minute native budget with `--wait-async`.

The service waits for the actual child process once. It does not use interactive
crash supervision or auto-relaunch a print-mode conversation. A draft report,
assistant stop message or normal-looking exit metadata cannot prove success.
Completion requires the actual child exit code zero, its normal saved terminal
state with no pending tool calls, and the incident report. The native fixed title
record before the session header is accepted, as are older header-first files.
A recorded process exit without a transcript consumes the attempt and cannot launch
a replacement conversation. Native async deadline, authentication or provider
failures remain visible as failed attempts. Temporary
monitor inspection errors cannot terminate the separate running repair service.

After a confirmed host reboot, one provably unfinished root conversation can
resume once using its exact saved file and an explicit continuation prompt.
Completed/stopped work, a cancellation marker, multiple root sessions or an
uncertain outcome prevents automatic resume. A rejected unit dispatch can retry
the same pending resume claim after authoritative absence; starting the native
resume consumes the one allowance. No replacement conversation is created for a
saved interrupted session. Unverified asynchronous completion remains interrupted.

All launch paths, session files and briefs live under persistent home directories.
The default source reference is `odroidn2:/home/ultra/homeassistant`; execution does
not depend on an implementation worktree or a `/tmp` artifact. Before any repair
mutation the brief requires rechecking live reliability and the incident identity.
Physical recovery is still determined by the care integration, not an OMP report.

Each run prints its private log path under `/tmp/ha-plant-sensor-recovery/`; an
unsafe pre-existing directory causes selection of a separate owned private
fallback. That fallback is reused across timer runs; it is not reallocated every
minute. Only owned execution logs older than two days are expired. Full native output is
kept privately with the incident, and process receipts contain sanitized status.

## Verification

Run the focused stdlib test file:

```sh
python3 tests/plant_recovery/test_recovery.py
```

It covers bounded first aid, healthy saturation, restart limits, transport delays,
actual measurement spacing, retained evidence, malformed payloads, missing optional
entities, durable deduplication, failed dispatch, real process completion, explicit
same-session reboot continuation, live cgroup ownership and private log fallback.
Full suites and adversarial runs use established CI or RunPod, not production hosts.
