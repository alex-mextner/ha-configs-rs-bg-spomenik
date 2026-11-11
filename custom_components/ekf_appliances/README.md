# EKF Appliances

Original local Home Assistant integration for the living-room Toshiba projector and Haier HSU-12HT303/R2 air conditioner, with an optional Samsung TV adapter. It uses the existing EKF Tuya Local remote. The Samsung adapter reuses the existing Samsung device and its native LAN media controls.

## Configuration

Add **EKF Appliances** in Devices & services. Select:

| Field | Entity domain | Purpose |
|---|---|---|
| `remote_entity_id` | `remote` | Local EKF IR transmitter |
| `temperature_entity_id` | `sensor` | Actual room temperature |
| `projector_power_entity_id` | `binary_sensor` | Observed projector HDMI connection |
| `projector_on_script` | `script`, optional | Defaults to `script.projector_turn_on` |
| `projector_off_script` | `script`, optional | Defaults to `script.projector_turn_off` |
| `tv_entity_id` | native Samsung `media_player`, optional | Existing Samsung LAN backend; empty disables the TV adapter |

Existing entities can be selected while unavailable. Reconfigure preserves identities when the selected connections still refer to the same appliances. No setup, reload, state restoration, or sensor update sends IR.

The temperature sensor must report a recognized temperature unit; unknown, unavailable, malformed, unitless, and non-finite values produce no current-temperature reading.

## Devices and capabilities

**Haier:** full-state off/cool/heat/dry/fan/auto; 16–30°C in 1°C steps; auto/low/medium/high fan; off/vertical swing; none/sleep/boost presets. Boost is available in cool/heat. Switching to another active mode clears boost. While off, changing desired settings does not turn the AC on. Explicit off always sends the model-specific off command, even when the assumed state is already off.

The first setup defaults to off, 24°C, auto fan, no swing/preset, and previous mode cool. Later reloads restore the previous assumed mode and desired settings without transmitting. The native target temperature is persisted separately from HA's display-unit conversion. A legacy Comfort preset restores as None while preserving the other valid settings; restoration still sends no IR commands.

**Projector:** media power, relative volume, and play/pause toggle; twelve raw buttons for power, play/pause, source cycle, up/down/left/right, menu, back, volume down/up, and mute. Mute is a raw toggle button, not an absolute media-player mute capability. No absolute volume, discrete input selection, or unverified OK command is invented.

Media power calls the existing named scripts and waits for measured HDMI feedback. Repeated requests share the pending transition, while an opposite request waits and rechecks the observed state. The existing off script sends a second POWER three seconds later only if HDMI still reports on. The compatibility helper follows measured feedback; it does not supply the player state.

## Optional Samsung TV adapter

Select the existing **Samsung Smart TV** media player in reconfigure. The selector accepts only a registered native Samsung entity with its existing device and saved host/model. It does not operate the TV. Omitting or clearing the field leaves the projector and AC unchanged and creates no TV adapter.

The adapter uses one discrete Samsung32 ON (`E0E09966`) or OFF (`E0E019E6`) frame through the shared EKF transport. Both were verified on this UE48J5500 with the native integration enabled. Frames use the same timings as the verified existing ON automation: 4480 µs header, 560 µs marks/zero spaces, 1680 µs one spaces, 108080 µs total including the final space. They are generated from address/command bits and checked against a literal decoded timing fixture; opaque base64 is not copied into the implementation. An explicit toggle from unknown state sends one `E0E040BF` frame.

A passive GET of the selected host's `/api/v2/` endpoint on port 8001 runs every five seconds with a two-second timeout. A 200 response whose `device.modelName` matches the saved native model reports on. Timeout or connection refused reports off. Unexpected status, malformed data, another model, DNS failure, or another ambiguous error reports unknown. This measures network reachability, not the physical panel: a LAN failure can resemble standby. It never opens a control WebSocket. The native integration's slower power status cannot override this observation.

Power requests share a pending task until feedback or a 35-second deadline. Repeated on/off and power-button clicks do not resend while that task is pending. An opposite request waits and rechecks feedback. Known target states are idempotent; verified discrete ON/OFF can also be requested from unknown state. Commands never optimistically change the displayed state. Failure to observe the requested state raises a clear error without another transmission. Caller cancellation keeps the pending guard; removing the entity cancels the transition, and config-entry unload stops the coordinator. Canonical entity renames keep polling active.

Source, volume, mute, channel and media requests are forwarded through public HA services to the selected native backend. The supported feature mask is limited to its existing capabilities. Native ON/OFF services are never called. If passive feedback is unknown but the native backend still reports on, its non-power controls remain usable; a known passive off state takes precedence. The adapter does not improve or claim physical verification of native non-power commands.

The adapter attaches to the already validated public `Entity.device_entry`, preserving the original Samsung device ID, ownership, area, name and connections. In this HA version, registering the same identifiers through another config entry would create a separate device. Its unique ID is `<ekf_entry_id>_samsung_<native_unique_id>`. Rename the native player to a technical backend first, select that backend in reconfigure, then assign the adapter the original canonical player ID through HA's entity-registry API. Keep the native remote and existing scripts. No device merge, registry-file edit, or deletion is required. The separate `packages/samsung_tv_ekf.yaml` marks the canonical TV as slow for Alice; it does not change the existing power automation.

## Stable identities and migration

| Entity | Unique ID |
|---|---|
| Haier climate | `<entry_id>_haier` |
| Projector player | `<entry_id>_projector` |
| Projector command button | `<entry_id>_projector_<key>` |
| Optional Samsung player | `<entry_id>_samsung_<native_unique_id>` |

Button keys: `power`, `play_pause`, `source`, `up`, `down`, `left`, `right`, `menu`, `back`, `vol_down`, `mute`, `vol_up`.

Device identifiers are `(ekf_appliances, <entry_id>_haier)` and `(ekf_appliances, <entry_id>_projector)`. Both link through the configured remote's device when its registry identifiers are available. Initial entity IDs depend on HA naming and existing registry entries; migrate by unique ID.

The migration operator must retire the old ESPHome entities through HA APIs and assign the new buttons the existing `button.projector_<key>` IDs **before exercising projector power scripts**. Preserve the helpers/scripts/automations in `packages/projector.yaml`; remove only the old universal player definition. Assign the new player `media_player.projector` and climate `climate.kondei_1` after freeing those IDs. Assign the living-room area and update the existing Alice export. Never edit `.storage` or runtime databases.

## Transmission and state limits

Raw commands use standard `remote.send_command` with `b64:` unsigned little-endian 16-bit timings, one repeat, and zero service delay. Every frame includes its terminal low timing. A shared lock covers exactly one complete Haier frame per action and its waveform duration. The six-repeat workaround for the previous IR hardware is not used. A retained deadline prevents cancellation from allowing the next queued command to overlap the last frame.

A successful HA service call is not physical acknowledgment. Tuya Local may absorb some underlying connection errors; IR appliances provide no return channel. Climate settings therefore remain assumed; the projector and optional TV player instead report their observed HDMI/network feedback. Surfaced service failures abort the command and leave the last committed climate settings intact, but a partially delivered transmission may already have affected the appliance.

Wire formats are based on the existing ESPHome configuration and IRremoteESP8266 **2.9.0** [ir_Haier.cpp](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.cpp) / [ir_Haier.h](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.h). The independent encoders include a fourteen-byte off frame with Health disabled and extended-NEC address 0xBD00. Full-state commands follow [IRac::haierYrwo2](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/IRac.cpp): Power command byte 0x05 is set last, while the heat/cool mode bits remain 4/1. The old custom component’s Turbo/Health setter-order artifact is removed. No third-party integration code is copied.

## Verification

The focused suite uses real HA config flows, services, registries, and EntityPlatform lifecycle with an in-memory service boundary. **107 tests pass** on HA 2026.9.3 / Python 3.14.6, covering literal wire fixtures, invalid inputs, real sensor units, state restoration, failure behavior, command serialization, cancellation, idempotent projector scripts, gateway-linked canonical renaming of all fourteen entities, Health exclusion, legacy Comfort normalization, observed projector feedback, same-device Samsung attachment, canonical rename and probe lifecycle, passive HTTP classification, exact Samsung power vectors, and command coalescing.

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
