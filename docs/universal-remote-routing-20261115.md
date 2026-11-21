# Universal remote routing and shutdown

## Design

The mode authority is `RemoteMode` in the EKF integration. YAML retains the
verified button-to-command mappings and calls typed routing services. A single
sensor publishes complete effective targets; helper write ordering cannot leak
an intermediate projector or native state to announcements.

The controller uses physical capture timestamps and cancellable callback
ownership for two independent deadlines: the inclusive one-second INPUT double
window and exactly sixty seconds of inactivity. Single INPUT retains its original
pair snapshot. When INPUT selects the TV pair, an off TV stays off. Explicit app
and source commands can still wake it. The lease starts at the physical action, including
the first INPUT of a single, and every accepted universal-remote action renews it.
Display OFF or projector shutdown invalidates stale pending input and affected
manual focus. Unknown observations never mean OFF.

The universal remote reserves Power synchronously when the physical press is
captured. Repeated Power is rejected while either pair is starting or stopping:
it cannot cancel startup, reverse the requested direction, queue another toggle,
or wake the TV after automatic projector fallback. The captured admission expires
after five seconds if YAML never claims it. Once dispatched, caller cancellation
does not release the guard; actual paired/native confirmation or its bounded
failure ends the operation. The service boundary also bounds an unfinished call
at 150 seconds. Native feedback reconciliation and its late-acknowledgement fences
remain independent of remote admission. Tokenless compatibility preparation checks
the route without reserving or adopting a physical Power request.

Ordinary navigation, text, volume and transport keys are rejected for a busy,
confirmed-off, or unknown display. A rejection captured during a transition stays
rejected if YAML runs after confirmation. Explicit source/app selection may start its named idle pair. Single INPUT may
wake the projector when the TV is on; Power remains busy until all active pairs
settle. Independent AC, lighting and speaker controls remain available.

A single Audio press toggles playback. On the living-room Yandex Station,
Play resumes the current track or starts My Wave when the local Station reports
no current item, including when Alice is speaking with no track, radio or
playlist context. In cloud-only mode it requests resume, since
the missing track information cannot establish whether the queue is empty.
The receiver recognizes double Audio within 800 ms, even when both presses
and the following Play reach it before the automation runs. Playback, track and volume buttons keep
the speaker selected for sixty seconds after the latest press. Audio, Back and
Exit dismiss speaker mode immediately. Colour lights work without extending
speaker focus. Paired display Power also leaves speaker focus in place. Menu,
Home, Guide, Dash, Netflix, YouTube and Disney leave speaker focus and use the
normal display guard on the same press. Disney also does so on a hold. Playback,
arrows, OK and volume are routed to the owner selected at physical reception,
even if another button changes focus before dispatch. Apple TV in speaker mode
enables Bluetooth on the speaker. A short or held OK toggles music once.

A recognized command rejected by a guard emits the short quiet rejection cue
through remote_announcements.reject. Unsupported keys/text operations, unavailable
mute or climate data, and failed native text responses use the same cue.
Receiver-provided logical press identities give OK/OK-hold and held navigation or
volume repeats one cue for the original press. Separate full-frame taps keep their
own identities, including rapid taps. A bounded automation-context bridge carries
that identity through nested AC and T9 scripts and failed native dispatches.
Receiver echoes and internal timer events
remain silent. Voice search uses the same per-pair guard before each key/text
step and stops at its first rejection; it does not implicitly wake a display.
Disney checks its captured projector admission before calling the source script.
A declined or empty source result gets one cue, and dependent episode work stops.

The selected receiver commits each mapped physical press synchronously, before
publishing the HA event. YAML retrieves its captured route from a bounded
256-entry cache and cannot replay the press. Only that receiver supplies trusted
physical timestamps; unknown or evicted event timestamps never renew a lease.
Before an INPUT or inactivity callback commits, the controller drains already
ready native input, so a second capture at exactly 1000 ms wins even when HA
processes it after the callback was due. A capture just before lease expiry is
compared with its physical time, then renews the lease for exactly sixty seconds.

Projector shutdown is a separate persisted latch. It is set synchronously before
the power worker can yield, and also by fresh non-echo OEM POWER observations.
Only explicit, fresh HDMI OFF newer than the request and any reload releases it.
The public projector entity exposes `shutting_down`; the effective target sensor
exposes `projector_shutting_down`. The guard is checked at the remote service
handoff, after the IR queue and receiver preparation, and in Kodi keyboard mutation
queues. Accepted internal shutdown confirmation IR remains permitted. This safety
latch is distinct from transient global Power admission: a failed projector
transition releases the TV once its active lifecycle ends, while commands aimed
at the projector still require fresh HDMI OFF to clear the latch. An OEM or direct
script OFF without a pair worker also blocks global Power for the existing
75-second shutdown window, measured from the recorded request time. An aged
persistent latch cannot retain that global block. Settling an owned OFF does not
start another window from its remaining latch.

## Evidence and validation scope

The old mapper emitted thirty INPUT events for thirty full frames at 110 ms
intervals, and twelve AUDIO events for a held full-frame burst. It also collapsed
two kernel captures 150 ms apart into the same wall-clock receive time. New
regressions establish one held mode press, distinct 150 ms captures and OEM
projector observation before universal-profile filtering.
An unrelated or transmitted echo frame between held INPUT frames also used to
reset the mapper and invent a second press. A separate per-mode physical-frame
gate now preserves silence accounting across those semantic resets.

The old YAML timeout could run up to 999 ms before its millisecond deadline,
leave native mode active, and never rearm. The new state tests cover fractional
expiry without another key, old callbacks after renewal, both displays ending
OFF, original-pair doubles at 1000 ms, sixty-second wake focus, atomic target
publication, and fresh TV doubles while the projector shuts down.
Review regressions additionally cover a 1000 ms second INPUT processed 20 ms
late without a premature wake, a key captured 100 ms before lease expiry and
processed 200 ms after it, bounded event deduplication, native ready-FD draining,
and cancellation of both waiting and subsequently queued Kodi text writes.

Focused checks use isolated source and in-memory hardware boundaries. Full
repository suites, coverage and heavier matrices belong to genuine CI/RunPod.
No test should toggle physical appliances or inject a live remote event.

## Issue 159 deployment and rollback

Install the following seven runtime files together from the reviewed merged commit.

- `packages/universal_remote.yaml`
- `packages/music_remote.yaml`
- `packages/remote_announcements.yaml`
- `custom_components/ekf_appliances/remote_mode.py`
- `custom_components/ekf_appliances/remote_control.py`
- `custom_components/remote_announcements/__init__.py`
- `custom_components/remote_announcements/services.yaml`

When checking announcements, use a source that reports playback for the 6/10
case. The Android TV Remote entity reports power, active app, volume and mute.
A native TV app without a Cast or Music Assistant playing report provides no
playback indication. If no other living-room source reports playback, its
confirmation uses 4/10.

Compare the seven live files with their expected originals and retain verified
backups. Run the installed `scripts/ha_safe_reload.sh` configuration check and
restart once. After startup, verify the `remote_announcement_playback_entities`
attribute on `sensor.universal_remote_target`, the `input_text.audio_pending_tap_ms` helper,
the `remote_announcements.adopt_label_volume` action, and the speaker and climate
entities. Observe the healthy process for 60 seconds. If rollback is required,
restore these seven verified originals together after checking for intervening changes. Preserve unrelated files and the production Git index.

## Issue 88 deployment and rollback

Review the scoped source diff and exact tested commit. Preserve unrelated staged
and unstaged production changes. Deploy only these 13 reviewed runtime files:

- `packages/universal_remote.yaml`
- `custom_components/ekf_appliances/remote_control.py`
- `custom_components/ekf_appliances/remote_mode.py`
- `custom_components/ekf_appliances/services.yaml`
- `custom_components/ekf_appliances/manifest.json`
- `custom_components/ekf_appliances/ir_decode.py`
- `custom_components/ekf_appliances/ir_receiver.py`
- `custom_components/ekf_appliances/native_ir.py`
- `custom_components/ekf_appliances/receiver.py`
- `custom_components/remote_announcements/__init__.py`
- `custom_components/remote_announcements/rejection.py`
- `custom_components/remote_announcements/services.yaml`
- `custom_components/remote_announcements/manifest.json`

Preserve the existing announcement core. Use `scripts/ha_safe_reload.sh` with the
release coordinator to validate the configuration and load the changed Python and
YAML-configured announcement service through one validated Home Assistant restart.
Read back the target sensor, projector shutdown attribute and integration errors.
Observe a fractional lease expiration without sending a key. Physical INPUT and
shutdown acceptance require separately coordinated user operation.

Rollback restores only the preceding reviewed versions of the changed routing
package, EKF and remote_announcements files, followed by the established validated
restart. Never edit the persisted shutdown Store or runtime databases to make
rollback appear successful. Keep the reviewed commit and validation report as the
audit record.
