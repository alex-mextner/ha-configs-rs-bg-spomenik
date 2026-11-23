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
4. If the integration still reports the incident after verification, deliver to the shared
   plant repair subscriber, at least ten minutes after first observation. A healthy
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

## Persistence and shared repair subscriptions

Private domain state remains under `~/.local/state/ha-plant-sensor-recovery/`,
atomically saved under the existing monitor lock. Sensor thresholds, two physical
refreshes, dedicated bridge restart limits and actual recovery evidence are
unchanged. The production backend delegates only agent dispatch to
`agent-sub publish --source plant-sensors --launch-profile plant-sensors`.

The shared broker reuses the current verified native subscriber or reserves,
starts an idle endpoint and binds it before delivering the immutable incident.
A lost publication response retries the identical envelope. Delivery means input
acceptance, never a repaired sensor. The controller checks current measurement
reliability before any new effect. A live or ambiguous pre-migration legacy worker
is reconciled before shared dispatch; the legacy host subcommand remains only to
finish those existing workers.

Install `ha_plant_repair_subscriber.py`, `ha_smart_home_common.py`, the reviewed
plant standing instructions at the exact absolute profile path (owned, mode 0600
or 0644), and a mode-0600 `plant-sensors` profile beside the Ubuntu controller. Enable `ha-plant-repair-subscriber.service` for the shared
native lifecycle event stream. A positively confirmed loss is recorded, then an
ordinary read-only sensor observation determines whether the same unresolved
incident needs a new event revision. Unknown liveness does not authorize a second
agent. Shared protocol version 1 is checked before accepting watch events or
delivery acknowledgements. A retained native loss takes precedence over synthetic
startup-vacancy reconciliation. If a read-only observation is temporarily
unavailable, the shared watch retains and redelivers its unacknowledged loss on a
concrete retry deadline; no new healthy polling loop is introduced. Domain
decisions are flushed before the native loss is acknowledged.

The existing care integration remains the only authority for physical recovery.
The standing brief forbids watering, calibration shortcuts, household audio,
other-device controls, broad service restarts, `.storage` or database edits, and
people-directed messages. Every normal controller run retains its private `/tmp`
log and two-day retention policy. Shared subscriptions and native process receipts
are owned by agent-tools, not a second HA registry.

## Verification

Run the focused stdlib test file:

```sh
python3 tests/plant_recovery/test_recovery.py
```

It covers bounded first aid, healthy saturation, restart limits, transport delays,
actual measurement spacing, retained evidence, malformed payloads, missing optional
entities, durable deduplication, failed dispatch, shared delivery ownership, and legacy live-cgroup reconciliation
for pre-migration workers. New dispatches use shared subscriptions; reboot does
not imply that a previously completed private session is resumed.
Full suites and adversarial runs use established CI or RunPod, not production hosts.
