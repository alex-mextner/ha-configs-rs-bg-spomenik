# Universal remote routing and shutdown

## Design

The mode authority is `RemoteMode` in the EKF integration. YAML retains the
verified button-to-command mappings and calls typed routing services. A single
sensor publishes complete effective targets; helper write ordering cannot leak
an intermediate projector or native state to announcements.

The controller uses physical capture timestamps and cancellable callback
ownership for two independent deadlines: the inclusive one-second INPUT double
window and exactly sixty seconds of inactivity. Single INPUT retains its original
pair snapshot and wake policy. The lease starts at the physical action, including
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
remain independent of remote admission.

Ordinary navigation, text, volume and transport keys are rejected for a busy,
confirmed-off, or unknown display. A rejection captured during a transition stays
rejected if YAML runs after confirmation. Explicit source/app selection and single
INPUT may start their named idle pair; Power remains busy until all active pairs
settle. Independent AC, lighting and speaker controls remain available.

A recognized command rejected by a guard emits the short quiet rejection cue
through remote_announcements.reject. Unsupported keys/text operations, unavailable
mute or climate data, and failed native text responses use the same cue.
Receiver-provided logical press identities give OK/OK-hold and held navigation or
volume repeats one cue for the original press. Separate full-frame taps keep their
own identities, including rapid taps. Receiver echoes and internal timer events
remain silent. Voice search uses the same per-pair guard before each key/text
step and stops at its first rejection; it does not implicitly wake a display.

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
at the projector still require fresh HDMI OFF to clear the latch.

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

## Deployment and rollback

Review the scoped source diff and exact tested commit. Preserve unrelated staged
and unstaged production changes. Deploy only the reviewed package and EKF files;
use the established HA validation/reload workflow with the release coordinator.
Read back the target sensor, projector shutdown attribute and integration errors.
Observe a fractional lease expiration without sending a key. Physical INPUT and
shutdown acceptance require separately coordinated user operation.

Rollback restores only the preceding reviewed versions of the changed routing
package and EKF files, followed by the established validated reload. Never edit
the persisted shutdown Store or runtime databases to make rollback appear
successful. Keep the reviewed commit and validation report as the audit record.
