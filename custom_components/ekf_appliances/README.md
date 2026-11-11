# EKF Appliances

Original local Home Assistant integration for the living-room Toshiba projector and Haier HSU-12HT303/R2 air conditioner. It uses the existing EKF Tuya Local remote and creates two separate HA devices. Samsung remains on its existing LAN integration.

## Configuration

Add **EKF Appliances** in Devices & services. Select:

| Field | Entity domain | Purpose |
|---|---|---|
| `remote_entity_id` | `remote` | Local EKF IR transmitter |
| `temperature_entity_id` | `sensor` | Actual room temperature |
| `projector_power_entity_id` | `input_boolean` | Existing assumed projector power |
| `projector_on_script` | `script`, optional | Defaults to `script.projector_turn_on` |
| `projector_off_script` | `script`, optional | Defaults to `script.projector_turn_off` |

Existing entities can be selected while unavailable. Reconfigure changes the selections without changing device/entity unique IDs. No setup, reload, state restoration, or sensor update sends IR.

The temperature sensor must report a recognized temperature unit; unknown, unavailable, malformed, unitless, and non-finite values produce no current-temperature reading.

## Devices and capabilities

**Haier:** full-state off/cool/heat/dry/fan/auto; 16–30°C in 1°C steps; auto/low/medium/high fan; off/vertical swing; none/sleep/boost presets. Boost is available in cool/heat. Switching to another active mode clears boost. While off, changing desired settings does not turn the AC on. Explicit off always sends the model-specific off command, even when the assumed state is already off.

The first setup defaults to off, 24°C, auto fan, no swing/preset, and previous mode cool. Later reloads restore the previous assumed mode and desired settings without transmitting. The native target temperature is persisted separately from HA's display-unit conversion. A legacy Comfort preset restores as None while preserving the other valid settings; restoration still sends no IR commands.

**Projector:** media power, relative volume, and play/pause toggle; twelve raw buttons for power, play/pause, source cycle, up/down/left/right, menu, back, volume down/up, and mute. Mute is a raw toggle button, not an absolute media-player mute capability. No absolute volume, discrete input selection, or unverified OK command is invented.

Media power calls the existing named scripts and waits for completion. A separate power-operation lock makes repeated on/off requests idempotent against the power helper. The raw power button sends one frame. The existing off script sends two power frames three seconds apart and retains its Kodi/source behavior.

## Stable identities and migration

| Entity | Unique ID |
|---|---|
| Haier climate | `<entry_id>_haier` |
| Projector player | `<entry_id>_projector` |
| Projector command button | `<entry_id>_projector_<key>` |

Button keys: `power`, `play_pause`, `source`, `up`, `down`, `left`, `right`, `menu`, `back`, `vol_down`, `mute`, `vol_up`.

Device identifiers are `(ekf_appliances, <entry_id>_haier)` and `(ekf_appliances, <entry_id>_projector)`. Both link through the configured remote's device when its registry identifiers are available. Initial entity IDs depend on HA naming and existing registry entries; migrate by unique ID.

The migration operator must retire the old ESPHome entities through HA APIs and assign the new buttons the existing `button.projector_<key>` IDs **before exercising projector power scripts**. Preserve the helpers/scripts/automations in `packages/projector.yaml`; remove only the old universal player definition. Assign the new player `media_player.projector` and climate `climate.kondei_1` after freeing those IDs. Assign the living-room area and update the existing Alice export. Never edit `.storage` or runtime databases.

## Transmission and state limits

Raw commands use standard `remote.send_command` with `b64:` unsigned little-endian 16-bit timings, one repeat, and zero service delay. Every frame includes its terminal low timing. A shared lock covers exactly one complete Haier frame per action and its waveform duration. The six-repeat workaround for the previous IR hardware is not used. A retained deadline prevents cancellation from allowing the next queued command to overlap the last frame.

A successful HA service call is not physical acknowledgment. Tuya Local may absorb some underlying connection errors; IR appliances provide no return channel. Entities therefore expose assumed operating state. Surfaced service failures abort the command and leave the last committed climate settings intact, but a partially delivered transmission may already have affected the appliance.

Wire formats are based on the existing ESPHome configuration and IRremoteESP8266 **2.9.0** [ir_Haier.cpp](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.cpp) / [ir_Haier.h](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.h). The independent encoders include a fourteen-byte off frame with Health disabled and extended-NEC address 0xBD00. Full-state commands follow [IRac::haierYrwo2](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/IRac.cpp): Power command byte 0x05 is set last, while the heat/cool mode bits remain 4/1. The old custom component’s Turbo/Health setter-order artifact is removed. No third-party integration code is copied.

## Verification

The focused suite uses real HA config flows, services, registries, and EntityPlatform lifecycle with an in-memory service boundary. **50 tests pass** on HA 2026.9.3 / Python 3.14.6, covering literal wire fixtures, invalid inputs, real sensor units, state restoration, failure behavior, command serialization, cancellation, idempotent projector scripts, gateway-linked canonical renaming of all fourteen entities, Health exclusion, and legacy Comfort normalization.

Run on the ODROID using the installed image, with no network or live config mount:

```bash
docker run --rm --pull never --network none --read-only \
  --tmpfs /tmp:rw,nosuid,size=64m --memory 512m --cpus 1 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --mount type=bind,src=/home/ultra/work/ekf-appliances-20261111,dst=/work,readonly \
  --workdir /work --entrypoint python \
  sha256:d8922685169707fd91e8b9729902d975f06157d005e422874d201e0261dda196 \
  -m unittest discover -s tests/ekf_appliances -p 'test_*.py' -v
```

The unrelated repository-wide suite needs absent development environments and was not installed or run. Runtime UI verification and appliance reception checks belong to the deployment task.

## Health/Filter compatibility

Health/Filter is disabled in every Haier frame, including OFF. The unsupported Comfort preset is neither offered nor accepted. [Upstream issue #1894](https://github.com/crankyoldgit/IRremoteESP8266/issues/1894) reports a Haier model displaying the cooling indicator despite a correctly encoded heat mode when byte 3 bit 1 (Health) is enabled. Its original working heat capture is an independent regression fixture; it is a different model and does not by itself establish this unit's physical behavior. Heat remains native mode 4, cool mode 1, and full-state Power command 5.
