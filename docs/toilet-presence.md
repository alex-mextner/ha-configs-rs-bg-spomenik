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
door-open state. Door motion can trigger it, but that is not a door contact.
Use a magnetic contact if actual opening/closing is required. A contact alone
does not establish occupancy with an open or ajar door.

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
