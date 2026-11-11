# EKF living-room appliances design

## Outcome

Create separate Toshiba projector and Haier HSU-12HT303/R2 devices using the configured EKF remote. The Samsung LAN integration remains the television. This branch supplies code only; the root task owns deployment, HA registry migration, Alice exposure, and physical verification.

## Interfaces

- Original integration domain: `ekf_appliances`; no third-party integration copying or new dependencies.
- Target Home Assistant 2026.9.3 / Python 3.14.
- Config selects `remote_entity_id`, `temperature_entity_id`, `projector_power_entity_id`, and optional on/off script entities defaulting to `script.projector_turn_on` and `script.projector_turn_off`.
- One runtime lock serializes whole transmissions. Standard blocking `remote.send_command` receives `b64:<unsigned little-endian 16-bit pulse durations>`.
- Setup, restoration, reload, sensor updates, and availability checks never send commands.
- Stable separate device identifiers; gateway link from the configured remote's registry device when available.
- Suggested initial IDs: `climate.ekf_haier`, `media_player.ekf_projector`, `button.ekf_projector_<command>`.

## Haier

Expose off/cool/heat/dry/fan_only/auto; 16–30°C in integer steps; fan auto/low/medium/high; swing off/vertical; presets none/sleep/comfort/boost. Current temperature comes only from the configured sensor, converting temperature units; unavailable, malformed, unknown, or non-finite readings become None. Restore desired settings and previous non-off mode without sending; default off. Target changes while off store the desired value without enabling the AC. Validate before state changes. Commit assumed operating state only after successful transmission; failures raise HA errors.

Send complete fourteen-byte YRW02 state. Off bytes: `A6 90 00 02 00 A0 00 80 00 00 00 00 05 5D`. Byte 13 is sum(bytes 0–12) modulo 256. Header timings: 3000,3000,3000,4300µs. Send 112 bits MSB-first, mark 520µs and spaces 1650µs/650µs for 1/0; footer 520µs plus a 5000µs terminal space for Tuya. Exactly one complete frame per action with its waveform duration reserved; no long gap inside the frame. A retained deadline protects the final waveform after cancellation.

Sources: existing `/home/ultra/esphome/components/haier_acyrw02`; IRremoteESP8266 2.9.0 [header](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.h) and [implementation](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.cpp). Independently implement these wire-format facts without vendoring that library.

## Projector

Twelve verified extended-NEC commands at address 0xBD00, one frame per action. Media controls supply relative volume and play/pause toggle; mute remains a raw button, no absolute volume, invented OK, or named input selection. Power mirrors the selected input boolean; unavailable/unknown helpers make the player unavailable. Media turn_on/turn_off invoke existing scripts, preserving double-press off and Kodi/source effects. Raw power button sends one frame; existing scripts update the helper after successful button actions.

## Migration contract

Root renames new buttons to the existing `button.projector_*` IDs after retiring old ESPHome buttons through HA APIs. Before exercising player power, confirm scripts resolve to these new buttons. Remove only the old universal player block from `packages/projector.yaml`; retain scripts/helpers/automations and migrate the new player ID. Retire the old AC entity before assigning `climate.kondei_1`. Root assigns rooms and refreshes Alice. The root task prepared a package patch that removes only the old universal player. This worker performs no live migration.

## Verification

Use stdlib unittest plus a separate no-network HA-image container with a read-only worktree mount. In-memory HA services replace the appliance boundary; no production token/config or network is used. Cover golden vectors, validation, burst serialization, restoration, sensor handling, failure rollback, selectors, and startup silence. Physical IR delivery remains untested.

## User correction and protocol repair

The previous six-frame repetition was a workaround for old IR hardware, not a protocol requirement; the EKF sends one frame. Active full-state frames use command byte 0x05 following IRac::haierYrwo2, which calls setPower last before send. Heat/cool mode bits remain 4/1; physical mode confirmation is a deployment check. Device links use current HA via_device_id, avoiding legacy via_device failures during core entity renaming.
