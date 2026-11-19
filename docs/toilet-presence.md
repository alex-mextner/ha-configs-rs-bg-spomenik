# Toilet presence sensor

`esphome/toilet-presence.yaml` targets the photographed Seeed XIAO ESP32-C3 and
compact 24 GHz mmWave expansion board for XIAO (SKU 101010001). This board uses
the built-in ESPHome `ld2410` component; it is not an MR24HPC1.

The files do not calibrate the radar at boot or operate a light. Hardware
commissioning and Home Assistant entity binding are required before enabling
light control. Adding or compiling these files does not flash a device.

## Configuration and identity

- `esphome/toilet-presence.yaml`: board, identity, UART and per-device API key.
- `esphome/libs/base.yaml`: WiFi, encrypted native OTA and diagnostics.
- `esphome/libs/ld2410.yaml`: native driver, freshness and calibration controls.

The node is `toilet-presence`, friendly name `Toilet Presence`. Normal first
adoption produces the following IDs. Verify the actual IDs after adoption;
existing entities or renames can change them.

| Entity | Meaning |
| --- | --- |
| `binary_sensor.toilet_presence_occupancy` | Fresh presence, including still targets |
| `binary_sensor.toilet_presence_moving_target` | Fresh moving-target classification |
| `binary_sensor.toilet_presence_still_target` | Fresh still-target classification |
| `binary_sensor.toilet_presence_radar_reports_healthy` | A valid periodic report arrived within five seconds |
| `binary_sensor.toilet_presence_connectivity` | Device/native API connectivity |

Bind the controller to the verified occupancy and health entities. Unknown and
unavailable are neither clear nor occupied.

## Hardware and serial interface

| XIAO ESP32-C3 connection | Radar connection |
| --- | --- |
| 3V3 and GND | Sensor power and ground |
| D3 / GPIO5, controller TX | Radar RX |
| D2 / GPIO4, controller RX | Radar TX |
| D10 / GPIO10 | Optional OUT, unused by this configuration |

Stack the module in Seeed's prescribed orientation, with the radar antenna
facing outward. The flexible 2.4 GHz antenna belongs to the XIAO WiFi radio.

The schematic powers the radar from 3V3 and marks the 5 V header contact
`5V/NC`. An overview table says 5 V, but the product specification, hardware
description and schematic identify 3.3 V for this expansion board. Do not apply
MR24HPC1 power instructions.

UART is configured for 256000 baud, eight data bits, no parity and one stop bit.
The board protocol manual gives this factory rate; Seeed's ESPHome tutorial also
shows a 9600-baud example. Check an already configured module and set
`radar_baud_rate` to match it. This firmware does not change the radar baud rate.

## Credentials and version

Private ESPHome secrets are `wifi_ssid`, `wifi_password` and the unique
`toilet_presence_api_key`. The authoritative live ESPHome directory is
`odroidn2:/home/ultra/esphome`, mounted at `/config` in
`homeassistant-esphome-1`. These secrets are separate from Home Assistant's.

Keep real values outside git, CI, logs and artifacts. CI uses dummy values only.
The helper can inventory required secret names without printing their values:

```sh
python3 scripts/prepare_esphome_secrets.py --config esphome/toilet-presence.yaml --source-secrets /home/ultra/esphome/secrets.yaml
```

On the host that owns that private secrets file, add
`--output-dir /home/ultra/esphome-private/toilet-presence --prepare --harden-source`
to prepare a new private deployment bundle. The helper creates a 0700 directory,
0600 secrets file, and a missing per-device API key locally. It refuses to
overwrite an existing output directory; source secret bytes remain unchanged.
It does not connect by SSH or flash a device.

The prepared bundle's `secrets.yaml` is the durable device key store. For later
rebuilds, source `/home/ultra/esphome-private/toilet-presence/secrets.yaml` and
choose a new output directory. Reusing the original WiFi-only source could
generate a different API/OTA key and lose the installed device's identity.

The configuration requires ESPHome 2026.9.1 or later; use the repository's pinned
validation version for a reproducible build.

Native OTA declares `encryption: {}`. In ESPHome 2026.9.1 it inherits the static
API key and requires encrypted OTA. This is for the new device's initial USB
installation, not a migration procedure for an existing flashed node. Importing
this base package must not change the corridor device's OTA scheme or keys.
Retain the new device's API key securely before its first flash.

There is no fallback access point, web server or legacy shared OTA password.

## Freshness and failure behavior

The three LD2410 binary sensors each use `timeout: 5s` as their first and only
filter. Installed ESPHome 2026.9.1 source was inspected:

1. `LD2410Component::handle_periodic_data_()` publishes each binary state for
   every accepted periodic report, including an unchanged Boolean value.
2. `TimeoutFilter::input()` explicitly bypasses input deduplication. Every report
   refreshes its timer.
3. Timeout calls `invalidate_state()`; the native API sends
   `missing_state=true`, not false.
4. The next report restores state even if its Boolean value matches the last
   value before the outage.
5. LD2410 numeric fields use `SensorWithDedup` before publishing. Numeric
   `on_raw_value` callbacks cannot reliably heartbeat unchanged reports.

At boot, occupancy remains unknown until a report arrives. UART loss with WiFi
still alive makes occupancy unknown within five seconds and turns report health
off. Network/API loss makes the device unavailable in Home Assistant. Recovery
requires a fresh report; a cached state never proves departure.

Do not prepend lambda, settle, debounce or delayed filters to the timeout:
deduplication can suppress unchanged reports. Do not append delayed-off:
scheduled vacancy could outlive report loss. Apply release hysteresis in the
controller and cancel it when input becomes unknown/unavailable.

Numeric distances and energies are diagnostics and can retain previous readings.
Interpret them only while report health is on; they are not liveness signals.

## Calibration

All gates `g0`–`g8` expose moving/still energy and separate thresholds. The
maximum moving/still gates, radar vacancy timeout and distance resolution are
configurable. Existing module values are read; no thresholds are forced.

1. Mount firmly, aiming inward and somewhat down toward a seated torso. Avoid
   aiming through the doorway. Prefer a geometry where the hallway is outside
   the main beam or farther away than the occupant.
2. With the final enclosure fitted, enable engineering mode and measure the
   empty room, including ordinary fan operation.
3. Sit quietly in the actual position and record still energies. Waving a hand
   is not a substitute for this measurement.
4. Repeatedly pass the open and ajar doorway without entering; compare the
   readings with occupied measurements.
5. Limit both maximum gates to necessary coverage and tune moving/still
   thresholds independently. Higher thresholds mean lower sensitivity.
6. Repeat occupied and hallway checks after changes. Disable engineering mode
   when detailed calibration telemetry is no longer needed.

Default gate spacing is 0.75 m with about 6 m nominal range. The documented
0.2 m mode reduces nominal range to about 1.6 m; consider it only if the seated
person remains covered. Neither mode supplies angular zones or exact walls.

A short delayed-on filter alone cannot reject a brief hallway passage because
the radar's own vacancy timeout can hold presence afterwards. If the useful
corridor and seated-person readings overlap, change placement or add a separate
entry/door sensor. Simply raising thresholds can lose a still occupant.

## Door and other rooms

The board exposes scalar distances and energies, not X/Y, tracked people or a
door-open state. Door motion can trigger it, but that does not establish an
open/closed state. A magnetic contact is one option for a separate door input;
it alone does not establish occupancy with an open or ajar door.

The pictured HC-SR04 is also a possible experiment: a fixed mounting might
distinguish the door leaf's distance in selected positions. This depends on
geometry, clean echoes and tests across the full door swing. A missing or
invalid echo means unknown, not open or clear. It is not a reliable occupancy
sensor and is not enabled by this configuration. Standard HC-SR04 Echo is 5 V
logic and needs level reduction before connection to a 3.3 V ESP32 input; see
[Adafruit's pinout guide](https://learn.adafruit.com/ultrasonic-sonar-distance-sensors/pinouts).

Radial distance bands cannot reliably identify kitchen, living room and hallway
when their distances overlap. Restricting this radar to the toilet also removes
the proposed 2–4 m approach coverage.

A separate LD2450-based tracker can supply X/Y zones and up to three targets in
a shared visible area. Test its long-stillness behavior; do not automatically
replace the static-presence toilet sensor with it. Walls, corners and doorways
may require separate sensors.

## Physical acceptance, still pending

These checks are required after authorized installation; they have not been
performed merely by source inspection or CI compilation.

| Scenario | Required observation |
| --- | --- |
| Boot without radar | Occupancy unknown, never a fabricated clear/occupied event |
| Identical empty reports for more than five seconds | Valid clear state, health remains on |
| Identical occupied reports for more than five seconds | Valid occupied state, health remains on |
| Disconnect UART while WiFi stays connected | Occupancy unknown and health off within five seconds |
| Reconnect with the same previous Boolean state | Fresh state restored and health on |
| Lose WiFi/API while occupied | HA unavailable; controller does not infer departure |
| Extended quiet sitting, including night use | Occupancy retained without deliberate movement |
| Hallway passes, door open and ajar | No accepted toilet entry |
| Normal exit and brief re-entry | Correct release/cancellation and recovery |
| Fan and door movement in an empty room | No persistent false occupancy |

Record placement, firmware version, resolution, thresholds, maximum gates,
native vacancy timeout and these observations before automatic control is
enabled. Keep manual control available throughout commissioning.

## Reusing the existing corridor fade

The running source inspected for this work was
`odroidn2:/home/ultra/esphome/esp32-c3-light.yaml`. It uses a configurable
fade-in followed by a gradual decline over 30 minutes in 10-second transitions,
then a one-second final fade to off. Manual off starts a separate one-minute
motion block. This work does not change that live file or its keys, network
configuration, firmware, entry policy or cooldown.

`esphome/reference/esp32-c3-light.yaml` preserves a sanitized, inactive snapshot
of that source for comparison. Its device identity, static networking, original
OTA scheme and existing controller behavior are retained. Literal credentials
are replaced with private `!secret` references; these aliases need their original
values mapped privately before any future deployment. This snapshot is not a
build or deployment entry point, and does not import the new base package or
rotate the corridor's credentials. The compile fixture below is separate.

`esphome/libs/gradual_fade.yaml` extracts the decay operation for reuse.
`fade_profile.hpp` supplies its linear curve. The caller selects the light through
`fade_light_id`, commands any entry brightness, waits for that entry transition
if one was requested, then runs:

```yaml
- script.execute:
    id: gradual_fade
    duration_ms: 1800000
    floor_level: 0.10
```

The fade reads `current_values.as_brightness`, including the interpolated on/off
state multiplied by brightness, freezes that level and ends at
`min(start, floor)` before switching off. Thus an initial 5% night level is
not raised to a 10% floor. Restarting starts a complete new duration from the
current brightness. A fully-off light is left off.

`script.execute: gradual_fade_stop` cancels the queued decay and freezes an
ongoing native transition at its current level. If the light was manually
commanded off, it does not turn it on again. An adapter must use this operation
when taking ownership for a hold or manual override, rather than stopping only
the script while its last 10-second transition continues.

The package exposes `fade_internal_call` for synchronous light-call guards and
`fade_automatic_off` for the final asynchronous off callback. The light's
`on_turn_off` handler must acknowledge/reset `fade_automatic_off`; otherwise it
must stop the fade and apply the owner's manual-off policy. See the compile
fixture for that wiring. The package does not choose night/day brightness,
cooldown, occupancy or manual-override policy itself.

`esphome/tests/corridor-fade.yaml` is a compile fixture only. It deliberately
has no networking or OTA and must not be flashed. The native C++ regression
fixture `esphome/tests/test_fade_profile.cpp` checks hand-calculated day/night,
restart, off and endpoint cases. Full firmware compilation and the complete
regression set run in CI with no live credentials.

The Home Assistant light adapter follows the same 30-minute/10-second decay
contract. A Tuya light without native transitions requires stepped commands;
matching the schedule does not promise identical visible smoothness.

## Sources

- [Seeed board overview](https://wiki.seeedstudio.com/mmwave_for_xiao/)
- [Product specification](https://www.seeedstudio.com/Seeed-Studio-XIAO-24Ghz-mmwave-Human-Static-Presence-Module-p-6266.html)
- [Board schematic](https://files.seeedstudio.com/wiki/mmwave-for-xiao/24GHz-mmWave-for-xiao-SCH.pdf)
- [Installation manual](https://files.seeedstudio.com/wiki/mmwave-for-xiao/24GHz_mmWave_for_XIAO-Datasheet-V1.00.pdf)
- [Board UART protocol](https://files.seeedstudio.com/wiki/mmwave-for-xiao/24GHz_mmWave_for_XIAO-Usermanual-V1.00.pdf)
- [Seeed ESPHome guide](https://wiki.seeedstudio.com/mmwave_for_xiao_to_ha_bt/)
- [Working XIAO C3 configurations](https://github.com/Seeed-Studio/wiki-documents/discussions/802)
- [ESPHome LD2410 configuration](https://esphome.io/components/sensor/ld2410/)
- [ESPHome 2026.9.1 LD2410 source](https://github.com/esphome/esphome/blob/2026.9.1/esphome/components/ld2410/ld2410.cpp)
- [ESPHome 2026.9.1 timeout source](https://github.com/esphome/esphome/blob/2026.9.1/esphome/components/binary_sensor/filter.cpp)
- [Firsthand toilet installation](https://community.home-assistant.io/t/ld2410-esphome-tips/477058/61)
- [ESPHome LD2450](https://esphome.io/components/sensor/ld2450/)
- [LD2450 author on stillness limits](https://github.com/TillFleisch/ESPHome-HLK-LD2450/issues/14)
