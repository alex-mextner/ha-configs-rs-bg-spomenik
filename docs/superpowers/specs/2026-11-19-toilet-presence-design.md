# Toilet presence and reusable light control

Refs [#92](https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/issues/92).

## Intent and boundary

Prepare the Seeed Human Static Presence mmWave Sensor for XIAO (SKU101010001, LD2410 protocol) for a roughly 2 m² toilet and a future Tuya bulb. Detect seated still presence; use adjustable day/evening/night brightness and temperature; fade with the corridor's long timing. Preparation stays inactive until the real USB device and bulb are identified and radar calibrated. No live reload, flashing, light commands, runtime-state edits, or unrelated lamp enrollment.

The owner authorizes autonomous preparation without an optional approval round. Normal independent review and CI remain required. A commit or green software tests are not physical acceptance.

## Approaches and decision

Independent scripts in HA and ESPHome permit competing writes. An ESPHome controller observing direct HA/Tuya changes adds a distributed ordering boundary and unreliable context attribution. Selected: one controller per light. ESPHome owns local corridor output; a small HA integration owns the remote bulb. Reusable packages live under esphome/libs. Both adapters share behavior and parity cases rather than generated executable parameters.

## Sensor contract

Expected entities (verify after pairing):
- binary_sensor.toilet_presence_occupancy
- binary_sensor.toilet_presence_radar_reports_healthy

LD2410 report timeout invalidates occupancy after five seconds without reports. Validity requires occupancy on/off and radar health on. Unknown/unavailable never means vacancy. Confirm continuous valid absence separately, default15 seconds. Hardware timeout and software confirmation are additive. The sensor provides scalar distance/energy, not x/y, identity, door state or reliable room boundaries. No kitchen/globe/hallway zones are invented. Optional approach input is an already-classified Boolean for suitable future hardware.

## Behavior contract

- Proposed adjustable profiles: day06:00–19:59=70%4000K; evening20:00–22:59=20%2700K; night23:00–05:59=5% at the bulb's warmest supported temperature. Use HA local time, Europe/Belgrade in this installation. Apply on automatic entry; do not overwrite a manual setting or jump at profile boundaries.
- Manual off invalidates pending writes synchronously, stores a deadline and original-visit latch, then issues off. Repeated/already-off requests restart the300-second minimum.
- Auto-on is inhibited while minimum time is unfinished OR original visit is unfinished. Only continuous valid absence finishes that visit. If it ended and a later visit is active at expiry, automatic lighting may resume then.
- Explicit manual on overrides suppression. Manual brightness/color remains until confirmed vacancy, then normal vacancy fading applies.
- Fade from current usable level to min(start,10%) over1800 seconds, at10-second steps, then off. Night5% must never rise to10%. Local light uses interpolated current_values; remote uses reported brightness and absolute quantized targets.
- Approach alone never turns an off light on. If on, cancel fade and hold max(current,10%). On approach exit, with no room occupancy/manual suppression, restart a full fade from then-current level. Unknown approach pauses shutdown.
- Unknown radar, unavailable bulb and restart invalidate old work. Preserve physical state, do not infer an exit, and never replay an old command backlog.

## Controller and persistence

The HA LightEntity facade is the strong manual route: policy changes before awaiting storage/transport. Observe raw HA light.turn_off/toggle requests too, including API/already-off requests without user_id; tag own contexts separately. External state feedback uses bounded specific expected-command matching, never a broad ignore window. Direct/external cloud ordering is best effort; already-forwarded actions cannot be recalled. One serialized writer sends newest intent next, coalesces queued levels, and checks generation immediately before transport.

Use a pure policy reducer for visit, suppression, manual hold, fade and generation. Save suppression through HA's supported Store helper, never manual .storage edits. Runtime durations are monotonic; restore from UTC deadline. Restored unresolved visits require new continuous vacancy. Save policy changes, not fade ticks. Storage failures surface and inhibit automatic writes while manual off remains available.

Instances require distinct IDs/actuators; cannot target another facade. Disabled default is inert. Reject on/off-only targets for dimming; clamp temperatures to advertised bounds. Native transitions only when advertised; software steps use absolute levels with at most one in-flight service call. Local PWM and remote Tuya fading cannot promise identical visual smoothness.

## Files and ownership

Lighting worker: custom_components/presence_light/{__init__,policy,controller,light}.py, manifest.json; tests/presence_light/; examples/toilet_presence_light.yaml outside auto-loaded packages; this specification and corresponding plan. No config flow.

Sensor worker: esphome/toilet-presence.yaml, esphome/libs/* and sensor docs/tests; extract existing corridor1800-second/10-second decline and1-second final off.

Security worker: scripts/prepare_esphome_secrets.py, guard/tests/CI/.gitignore. Default helper inventories required key names only. Explicit --prepare creates a NEW0700 private output directory, copied config/includes and minimal0600 secrets.yaml; never overwrites. Source /home/ultra/esphome/secrets.yaml stays on odroid; add unique toilet_presence_api_key. Optional --harden-source chmod0600 preserves bytes. No real secret operations during preparation. Automatic private/tmp log is redacted. Guard rejects committed secret files, generated firmware/build paths and plaintext sensitive fields. CI uses fake secrets, ESPHome2026.9.1 and uploads no firmware.

## Acceptance and verification

Heavy/full/adversarial/integration tests only in actual CI/RunPod; bounded ordinary development checks are permitted locally. Required cases: both suppression clauses, second visit, repeated/already-off, restart/deadline, unknown interruption, unavailable bulb, in-flight manual off, stale final-off, coalescing, manual brightness, approach0/3/10/35%, multi-instance isolation. Parent independently reviews complete diff. Physical acceptance later requires identified XIAO USB and toilet bulb, calibrated seated/empty detection, and observed bulb fade/manual control.

## Primary references

- https://esphome.io/components/packages/
- https://esphome.io/components/sensor/ld2410/
- https://wiki.seeedstudio.com/mmwave_for_xiao/
- https://api-docs.esphome.io/classesphome_1_1light_1_1_light_state
- https://contributors.home-assistant.io/core/entities/light/
- https://github.com/home-assistant/core/blob/dev/homeassistant/components/tuya/light.py
- https://github.com/basnijholt/adaptive-lighting/blob/main/docs/advanced/manual-control.md
- https://www.home-assistant.io/docs/configuration/state_object/
- https://www.home-assistant.io/integrations/timer/
