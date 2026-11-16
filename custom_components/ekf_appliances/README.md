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

Existing entities can be selected while unavailable. Reconfigure preserves identities when the selected connections still refer to the same appliances. Setup itself sends no IR. After HA is running, the paired-power controller reconciles a known display/player mismatch from actual feedback. State restoration and unknown readings never represent a button press.

The temperature sensor must report a recognized temperature unit; unknown, unavailable, malformed, unitless, and non-finite values produce no current-temperature reading.

## Devices and capabilities

**Haier:** full-state off/cool/heat/dry/fan/auto; 16–30°C in 1°C steps; auto/low/medium/high fan; off/vertical swing; none/sleep/boost presets. Boost is available in cool/heat. Switching to another active mode clears boost. While off, changing desired settings does not turn the AC on. Explicit off always sends the model-specific off command, even when the assumed state is already off.

The first setup defaults to off, 24°C, auto fan, no swing/preset, and previous mode cool. Later reloads restore the previous assumed mode and desired settings without transmitting. The native target temperature is persisted separately from HA's display-unit conversion. A legacy Comfort preset restores as None while preserving the other valid settings; restoration still sends no IR commands.

**Projector:** media power, relative volume, and play/pause toggle; twelve saved raw buttons for power, play/pause, source cycle, up/down/left/right, menu, back, volume down/up, and mute. Mute is a raw toggle button, not an absolute media-player mute capability. No absolute volume or discrete input selection is assumed. A separate native OK button now sends extended NEC 00 BD 0D F2. This additional key comes from [IRDB Sharp/Unknown_GJ210 device0/subdevice189](https://github.com/probonopd/irdb/blob/master/codes/Sharp/Unknown_GJ210/0%2C189.csv), whose power, menu, arrows and volume codes match eight saved keys. It was not present in the ESPHome capture; its physical response still requires verification. Kodi OK uses native Input.Select independently.

Media power calls the existing named scripts and waits for measured HDMI feedback. Repeated requests share the pending transition. An accepted OFF while HDMI confirms ON immediately sets a persistent shutdown latch; new ON requests are rejected until a fresh HDMI OFF report clears it. The existing off script keeps its own second POWER three seconds later only if HDMI still reports on. The compatibility helper follows measured feedback; it does not supply the player state.

## Optional Samsung TV adapter

Select the existing **Samsung Smart TV** media player in reconfigure. The selector accepts only a registered native Samsung entity with its existing device and saved host/model. It does not operate the TV. Omitting or clearing the field leaves the projector and AC unchanged and creates no TV adapter.

The adapter uses one discrete Samsung32 ON (`E0E09966`) or OFF (`E0E019E6`) frame through the shared EKF transport. Both were verified on this UE48J5500 with the native integration enabled. Frames use the same timings as the verified existing ON automation: 4480 µs header, 560 µs marks/zero spaces, 1680 µs one spaces, 108080 µs total including the final space. They are generated from address/command bits and checked against a literal decoded timing fixture; opaque base64 is not copied into the implementation. An explicit toggle from unknown state sends one `E0E040BF` frame.

A passive GET of the selected host's `/api/v2/` endpoint on port 8001 runs every five seconds with a two-second timeout. A 200 response whose `device.modelName` matches the saved native model reports on. Three consecutive endpoint timeouts/refusals/host-unreachable observations report off; a single missed response retains the previous observation. A timeout after HTTP connected is ambiguous, not off. Unexpected status, malformed data, another model, DNS failure, or another ambiguous error reports unknown. This measures network reachability, not the physical panel: a LAN failure can resemble standby. It never opens a control WebSocket. The native integration's slower power status cannot override this observation.

Power requests share a pending task until feedback or a 75-second deadline. Repeated on/off and power-button clicks do not resend while that task is pending. An opposite request waits and rechecks feedback. Known target states are idempotent; verified discrete ON/OFF can also be requested from unknown state. Commands never optimistically change the displayed state. Failure to observe the requested state raises a clear error without another transmission. Caller cancellation keeps the pending guard; removing the entity cancels the transition, and config-entry unload stops the coordinator. Canonical entity renames keep polling active.

Source, volume, mute, channel and media requests are forwarded through public HA services to the selected native backend. The supported feature mask is limited to its existing capabilities. Native ON/OFF services are never called. If passive feedback is unknown but the native backend still reports on, its non-power controls remain usable; a known passive off state takes precedence. The adapter does not improve or claim physical verification of native non-power commands.

The adapter attaches to the already validated public `Entity.device_entry`, preserving the original Samsung device ID, ownership, area, name and connections. In this HA version, registering the same identifiers through another config entry would create a separate device. Its unique ID is `<ekf_entry_id>_samsung_<native_unique_id>`. Rename the native player to a technical backend first, select that backend in reconfigure, then assign the adapter the original canonical player ID through HA's entity-registry API. Keep the native remote and existing scripts. No device merge, registry-file edit, or deletion is required. Home-specific Alice execution metadata is configured separately from this adapter.

## Stable identities and migration

| Entity | Unique ID |
|---|---|
| Haier climate | `<entry_id>_haier` |
| Projector player | `<entry_id>_projector` |
| Projector command button | `<entry_id>_projector_<key>` |
| Optional Samsung player | `<entry_id>_samsung_<native_unique_id>` |
| Universal remote target | `<entry_id>_universal_remote_target` |

Button keys: `power`, `play_pause`, `source`, `up`, `down`, `left`, `right`, `menu`, `back`, `vol_down`, `mute`, `vol_up`, `ok`.

Device identifiers are `(ekf_appliances, <entry_id>_haier)` and `(ekf_appliances, <entry_id>_projector)`. Both link through the configured remote's device when its registry identifiers are available. Initial entity IDs depend on HA naming and existing registry entries; migrate by unique ID.

The migration operator must retire the old ESPHome entities through HA APIs and assign the new buttons the existing `button.projector_<key>` IDs **before exercising projector power scripts**. Preserve the helpers/scripts/automations in `packages/projector.yaml`; remove only the old universal player definition. Assign the new player `media_player.projector` and climate `climate.kondei_1` after freeing those IDs. Assign the living-room area and update the existing Alice export. Never edit `.storage` or runtime databases.

## Transmission and state limits

Raw commands use standard `remote.send_command` with `b64:` unsigned little-endian 16-bit timings, one repeat, and zero service delay. Every frame includes its terminal low timing. A shared lock covers exactly one complete Haier frame per action and its waveform duration. The six-repeat workaround for the previous IR hardware is not used. A retained deadline prevents cancellation from allowing the next queued command to overlap the last frame.

A successful HA service call is not physical acknowledgment. Tuya Local may absorb some underlying connection errors; IR appliances provide no return channel. Climate settings therefore remain assumed; the projector and optional TV player instead report their observed HDMI/network feedback. Surfaced service failures abort the command and leave the last committed climate settings intact, but a partially delivered transmission may already have affected the appliance.

Wire formats are based on the existing ESPHome configuration and IRremoteESP8266 **2.9.0** [ir_Haier.cpp](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.cpp) / [ir_Haier.h](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/ir_Haier.h). The independent encoders include a fourteen-byte off frame with Health disabled and extended-NEC address 0xBD00. Full-state commands follow [IRac::haierYrwo2](https://github.com/crankyoldgit/IRremoteESP8266/blob/v2.9.0/src/IRac.cpp): Power command byte 0x05 is set last, while the heat/cool mode bits remain 4/1. The old custom component’s Turbo/Health setter-order artifact is removed. No third-party integration code is copied.

## Verification

Focused adversarial suites run only through the established CI/RunPod environment,
using HA 2026.9.3 and Python 3.14. They exercise real HA config flows, services,
registries, and EntityPlatform lifecycle with an in-memory device boundary.
Use the scoped source manifest and test report for the exact tested revision.
Do not run the suite or build images on Odroid; production verification uses
bounded health/readback checks and separately coordinated appliance acceptance.

Browser component proofs use the published custom-card source and labelled
synthetic HA fixtures. They establish DOM, capability, and asynchronous behavior;
they do not establish successful physical appliance reception or phone delivery.

## Health/Filter compatibility

Health/Filter is disabled in every Haier frame, including OFF. The unsupported Comfort preset is neither offered nor accepted. [Upstream issue #1894](https://github.com/crankyoldgit/IRremoteESP8266/issues/1894) reports a Haier model displaying the cooling indicator despite a correctly encoded heat mode when byte 3 bit 1 (Health) is enabled. Its original working heat capture is an independent regression fixture; it is a different model and does not by itself establish this unit's physical behavior. Heat remains native mode 4, cool mode 1, and full-state Power command 5.

## Fixed media pairs

The living-room TV is permanently paired with its Chromecast, and the projector with Kodi. Remote targeting is independent of these links: a temporary projector or native-menu mode cannot change the TV's playback owner. The canonical television selects only its active Music Assistant queue, Google Cast session or Android TV Remote backend. Kodi is never a TV playback or volume fallback.

The existing `tv_kodi_entity_id` key selects the projector's Kodi partner.
The optional `tv_source_entity_id` field is retained for configuration compatibility;
new setups can leave it empty. Remote mode is selected through the canonical target.

The packaged remote uses this home's canonical wrapper IDs
`media_player.ue48j5500_ue48j5500` and `media_player.projector`, together with
`sensor.universal_remote_target`. Native backend and playback-source renames
are tracked through the entity registry. Renaming a canonical wrapper or helper
requires updating the corresponding house-package references as well.

Power synchronization watches two independent observations for each pair. Samsung uses the matching passive identity endpoint; Chromecast uses Android TV Remote's pushed on/off state. Google Cast's off/idle playback state is never used as hardware power. The projector uses its matching HDMI display and Kodi uses the authenticated host bridge GET /kodi/status, which reports the actual systemd unit/process state. A loaded inactive/failed unit with a valid zero MainPID is OFF only when an independent Kodi process probe also confirms absence. Supervisor failure/result/exit status remain separate diagnostics; a normal SIGTERM shutdown can leave the wrapper at exit 143. Transitional units, inconsistent process evidence, malformed/read failures, stale polls, and network outages remain unknown.

Explicit canonical power requests drive both partners. An observed external transition (TV remote, Chromecast remote, projector remote, Kodi start or quit) drives its partner. Duplicates and echoes share one request. An opposite request takes precedence over delayed confirmation from the previous request, and each physical side is attempted only once per intent. A request finishes only after both states confirm it; failure is exposed through power_sync_error without optimistically changing either entity. Projector shutdown retains the existing fresh HDMI and 30-second stable-on guards before its two POWER frames.

On startup, the first complete pair of live observations is reconciled using the physical display; an explicit request during startup takes precedence. Unloading cancels monitoring and emits no power commands. Projector commands require known, fresh HDMI state; Samsung's discrete ON/OFF remains safe to request when its endpoint is unknown. The host Kodi launcher allows 90 seconds for HDMI so an externally started Kodi cannot prematurely select its headless windowing mode during projector boot.

The implementation does not modify the native Samsung, Android TV Remote or Google Cast integrations and contains no universal remote key mapping.

### Universal remote mode

`sensor.universal_remote_target` is the committed effective target. Its values are
`chromecast`, `tv`, `kodi`, `projector`, and `speaker`. A mode transition publishes
one complete pair/native choice; compatibility helpers are mirrors. Announcement
consumers must compare old and new sensor values, because lease refreshes also
update its attributes.

| Confirmed display power | Automatic target |
|---|---|
| Both OFF | Chromecast |
| Projector ON, TV OFF | Kodi |
| TV ON, projector OFF | Chromecast |
| Both ON | Chromecast |
| Unknown power | No OFF inference; Chromecast is the safe default |

A single INPUT changes the display group. It retains the existing wake rule:
when only the other group is ON, selecting an explicitly OFF display requests
that pair to wake using the captured power-intent revision. A double INPUT keeps
the original group and toggles Chromecast/TV or Kodi/projector. INPUT decisions
use the first physical press's snapshot; intervening OFF invalidates that snapshot.

Repeated full NEC frames and repeat-flag frames from a held INPUT do not create
additional mode presses. Separate INPUT frames need at least 150 ms separation
from the preceding frame; a valid double's two physical presses are at most
1000 ms apart, inclusively. Kernel reception timestamps survive buffered reads.
This accepts the 150 ms boundary without treating the usual 108–110 ms hold
cadence as two presses. Physical receiver acceptance remains a separate check.

Every explicit group/native choice expires exactly 60 seconds after the last
accepted universal-remote action, including INPUT, AUDIO, app, light and appliance
keys. Python callbacks own the fractional deadline and rearm early callbacks;
Home Assistant's rounded compatibility timer cannot strand a native mode.
Turning the selected display OFF ends the lease immediately even if the other
display is OFF. Selecting a display that was already OFF remains intentional
until a later OFF transition or the deadline. Wake/boot never extends the lease.

Projector shutdown suspends universal-remote traffic to both projector and Kodi,
including queued navigation, media, volume, text, app and power commands. The
effective group returns to TV/Chromecast and clears native mode. A dispatch guard
checks again after route resolution, and the IR writer checks after its queue and
receiver wait. The accepted shutdown's own confirmation frames remain allowed.
HA power services, the off script, raw projector POWER and fresh physical OEM
POWER observed by Odroid all establish the latch when HDMI currently confirms ON.
Matching transmitted IR echoes do not become physical remote observations.

The latch persists through integration/HA reload via Home Assistant's Store API.
Only a fresh explicit HDMI OFF report newer than the OFF request and reload can
clear it. Cached/restored OFF, unknown, unavailable, command failure and timeouts
cannot clear it. Audio mode continues to use double AUDIO and its existing
three-second inactivity timeout; while active the effective target is `speaker`.


Queued INPUT wakes call `ekf_appliances.wake_if_current` on the canonical media
entity with its captured `power_intent_revision`. The controller compares that
opaque token and records ON atomically, without awaiting between the two steps.
A newer request, including a coalesced duplicate OFF, rejects the old wake before
any appliance command. A new controller instance has a different token, so a
restored helper cannot authorize an old request after reload.

`last_power_intent` and `power_intent_revision` remain available after completion;
`power_sync_target` describes only a pending operation. Unchanged feedback,
volume and media metadata do not create a new power intent. Duplicate requests
advance intent history without restarting an in-flight physical transition.
The guarded service requires known display power and a usable IR transmitter
when the display needs waking; it never turns unknown power into permission.


### Odroid native IR reception

The receiver selector **Приёмник универсального пульта** chooses exactly one
semantic event source: `EKF` or `Odroid`. Existing installs keep their EKF opt-in.
The unchanged `event.ekf_universal_remote` event ID and the original ESPHome
profile remain the public remote interface; downstream TV/Kodi/Samsung
integrations do not know which physical receiver is selected.

Odroid opens its already exposed meson-ir `/dev/lirc0` read-only and nonblocking,
sets SCANCODE mode only on that file descriptor, and uses loop FD readiness.
It neither changes kernel protocols/keymaps nor grabs an input keyboard. A
passive native observer runs while EKF is selected, but cannot dispatch actions.
The explicit Odroid choice requires a connected receiver; `validated` separately
means this runtime has decoded a known profile frame. It is never inferred from
opening a device or from a transmit acknowledgement. Switching resets T9/hold
memory and discards input queued before the switch. There is no automatic
fallback that could cause both receivers to dispatch the same press.

Kernel monotonic timestamps and NEC repeat flags preserve rapid clicks and
holds. Unknown addresses/protocols, stale input, orphan repeats, and known
profile scancodes mapped to Linux keys are rejected. Disconnect closes the fd
and retries at a bounded two-second interval; unload cancels the retry and
closes any late-opened fd. Native diagnostics report the last protocol/scancode,
input lag, connection/error state, and suppression counters without retaining
a history of button input.

Explicit raw IR sends to the configured EKF hub suppress only matching decoded
frame fingerprints for their bounded transmit window. Chromecast API commands
do not pause native input. Native reception has no Tuya study/learning cycle and
never waits for the shared infrared transmit lock. Named external IR macros or
area-only targets cannot be fingerprinted from the call-service payload; the
canonical appliance paths use explicit EKF targets and raw frames.

Linux ABI references:
- https://www.kernel.org/doc/html/latest/userspace-api/media/rc/lirc-dev-intro.html
- https://www.kernel.org/doc/html/latest/userspace-api/media/rc/lirc-get-rec-mode.html
- https://github.com/torvalds/linux/blob/master/drivers/media/rc/ir-nec-decoder.c

Adversarial native-reader/selector tests live in `test_native_ir.py`; run them
only through the established CI/RunPod route, like the other HA integration
tests. The held Delete mapping also uses the existing repeat path; no extra
script delay is added.

## Phone keyboard

The authenticated WebSocket namespace `ekf_appliances/keyboard/` exposes
`subscribe`, `edit`, `commit`, and `close`. The card opens at
`/living-room/keyboard?pair=tv` or `?pair=projector`; automatic mode follows the
canonical remote controller. Opening the page sends no appliance command.
Projector/Kodi mutations are checked again inside the provider queue while the
projector shutdown latch is active.

Targets come from this entry's configured Android TV Remote and Kodi players.
The Android permission target is the unique remote in the configured native
entry. Both READ and CONTROL permission are checked before provider access,
before each mutation, and before every publication. Clients cannot select an
arbitrary HA entity. Field session IDs and opaque string revisions guard edits;
providers retain their integer revisions and share their transport lock with T9.
Old callbacks and write acknowledgements cannot revive a prior native instance.

Text and selection remain in volatile provider/coordinator/WebSocket memory.
They are not HA states, service data, events, notification bodies, URLs, or stored
drafts. Unknown and sensitive fields omit text and disable editing. Unicode is
bounded to 16384 UTF-16 units, including caret boundary validation. Narrow log
filters exclude keyboard frames from HA's generic WebSocket debug/overflow logs.

A safe focused field can notify registered mobile_app phones whose active user
has the same native target permissions. Services are resolved from the actual
registered service-to-webhook mapping, not guessed from device names. A usable
tracker belonging to that phone takes precedence over person presence; person
home is only a fallback when the phone has no usable presence observation.
Watches, TVs, and tablets are excluded. New eligible registrations are discovered
dynamically; no notification destination is invented for an unregistered user.

Each phone receives one text-free notification per field session with a stable
pair tag and an Open Keyboard link. Blur, disconnect, or permission withdrawal
clears that tag. Changed text/revision does not produce another notification,
and a failed push is not retried on every keypress. Service acceptance alone is
not evidence that a phone displayed the notification.

Kodi reuses the resolved origin/token of `rest_command.host_kodi_start` and the
existing native Kodi WebSocket connection. The Android adapter reuses its native
config entry's keyboard provider. Native integrations retain their own lifecycle;
unloading this integration detaches callbacks without changing appliance power.
