# Universal remote announcements

The package follows confirmed changes of `sensor.universal_remote_target`:
`chromecast`, `tv`, `kodi`, `projector`, `speaker`. Expiry of an initialized
temporary input mode announces the effective target once, including when that
target stays the same as control returns to automatic mode. While AUDIO mode
keeps the target at `speaker`, expiry of the underlying input lease stays quiet
and preserves any pending AUDIO-entry announcement. Leaving AUDIO mode announces
the actual target change once. Renewing an active lease, startup, unavailable
states and other repeated targets are quiet. Pending rapid changes coalesce; an
active announcement gets its volume/queue recovery first.

The fixed spoken labels match the requested remote names:

| Effective target | Spoken text |
|---|---|
| `chromecast` | `chrome cast.` |
| `tv` | `TV.` |
| `projector` | `проектор.` |
| `kodi` | `kodi.` |
| `speaker` | `Колонка.` |

The text is part of the cache key. A label change prepares only that missing clip;
unchanged labels retain their existing audio.

The exact installed voice is **Брачо**, Fish reference
`5cd1ffa5dbb4419cb5766435139c6d92`, entity `tts.bracho_bracho`. Missing or corrupt
clips are generated at preparation/startup, then converted once to mono 48 kHz
Ogg Opus at tempo 1.25. Route changes never synthesize or upload. Runtime audio and
private provider settings live in `.tts/remote_announcements/`, already ignored
by Git. Audio binaries must not be committed.

The current living-room Mini is `media_player.yandex_station_m0011hm00z86hg`.
`input_select.remote_announcement_backend` selects `Яндекс` or `AISIS`. A new
helper defaults to `Яндекс`; an existing choice survives HA restarts. This
selector is independent of the music remote's backend.
Explicit selection addresses that configured speaker regardless of HA room. The
configured AISIS Mini `media_player.aisis_speaker_m00zrw3008rajg` is in the studio;
selecting AISIS intentionally addresses it. Room `gostinaia` is only a filter for
automatic fallback. Update the entity setting when the physical speaker changes.

## Music, volume and latency

Both providers use the native speech channel and preserve the selected music
queue. Firmware controls music attenuation; a ducking level is not yet measured.
`music_mode: pause` is the explicit fallback: pause known playing content, speak,
then resume only unchanged original content if still paused.
Both modes use a two-second controller budget, including preparation commands,
speech completion and recovery. The controller reserves 0.6 seconds for recovery
and limits ordinary control acknowledgements to 0.4 seconds or the remaining
budget. Yandex speech may wait for its native acknowledgement until the remaining
work deadline; it cannot consume the recovery reserve or extend the total budget.
The native response must contain `status: SUCCESS` and no error. Rejected,
malformed or timed-out responses produce a fixed, nonprivate failure reason.
Speech completion waits use the cached duration plus 0.2 seconds, also bounded by
the remaining work deadline. A successful acknowledgement without an observed
speech-completion event remains `sent_unconfirmed`. Music recovery
runs independently of volume recovery, so a stalled volume acknowledgement does
not hold the original music paused indefinitely. Every recovery call rechecks
the original content or current volume before dispatch.

A target change invalidates an older attempt even if the target changes back
before its volume command finishes. Cancelled provider tasks remain tracked; if
a provider ignores cancellation, new announcements are blocked until it settles,
and shutdown still has a bounded wait. A command already accepted by a physical
speaker cannot be recalled. These deadlines therefore provide best-effort
controller recovery, not a guarantee of physical interruption or audible latency.

All five mode labels use 6/10 while any living-room source is playing and 4/10
otherwise. At the start of speech, the controller reads the routed Samsung TV
(active Chromecast, Android TV or Music Assistant session), Kodi on the projector,
and the living-room Yandex Mini. The controller counts a display only when both the display
and its selected player report active playback. Native Alice speech without underlying media does
not count. A content ID, title, media type, playlist or duration identifies underlying
Station content, using the same predicate as the remote's Play action. Each queued
label reads current playback after the preceding label or
quiet cue completes. Paused, idle, powered-on, buffering, unavailable
and restored states do not count as playing. An assumed cloud-only speaker state after a
disconnect does not raise the volume. The same rule applies when the
studio speaker is selected for announcements. A louder speaker is temporarily
turned down. The package configures the three source IDs on the target sensor through
`homeassistant.customize`. Older configurations without a source list keep their configured minimum
speech volume (default 5/10).

Restore the old volume only while the
current level still equals our temporary level and the
speaker was not muted by the user during speech. Yandex volume-zero mute is lifted
for speech and its remembered volume plus previous mute state are restored; user
changes during speech win. AISIS can restore native zero volume once its Quasar
volume API accepts integer levels 0..10. HA's normalized zero maps to native zero;
it must not be rounded up to the previous minimum level. A reported mute remains
unsupported because AISIS has no mute operation. Reported mute or a negative volume
reports `aisis_cannot_restore_muted_or_invalid_volume`. No Alice fallback is used.

**Deployment order:** install the reviewed AISIS Quasar zero-volume overlay and
verify the installed schema and actual native volume readback before deploying this
HA guard change. Older Quasar can acknowledge a request while discarding level zero,
so an HTTP/service acknowledgement is not restoration proof. After the restore call,
the controller waits for the original level or a distinct user volume or mute
within the existing recovery deadline. Missing readback reports
`recovery_unconfirmed` without retrying the command. The tests assert published device volume, and intervening human
volume changes still prevent restore.

Spoken labels and quiet cues on Yandex and AISIS keep a per-speaker recovery record until volume readback
confirms the original level, or a valid different human volume/mute state is observed.
While HA still reports the temporary speech level, another announcement is rejected
as `yandex_volume_recovery_pending` or `aisis_volume_recovery_pending`, even after the service acknowledgement completes.
Unrelated state attributes do not release this guard. If HA has accepted the original
volume but the final mute was never dispatched, the controller still waits for the
volume readback. When HA dispatches the mute, the original remembered level and
muted state must both be observed unless the user chooses a distinct volume.
The Yandex Mini remembers the last positive volume it reports. The controller
waits for that report before sending mute. A late report has at most two extra
seconds beyond the label’s two-second foreground budget. Losing ownership,
shutdown or expiry ends the continuation. A later echo cannot trigger a write.

Before changing the speaker level, the controller checks that its original level
and mute still match. Speech starts only after the speaker reports the requested
level with mute off. If that report misses the speech deadline, the label is
skipped. A label needing the same level may share the accepted write and wait for
its report; this does not renew that write's original uncertainty deadline.
A conflicting label is rejected and a quiet cue is dropped with the pending
recovery reason. Neither is automatically retried.

While the initial write remains unobserved, a distinct reliable human level or
mute becomes the recovery baseline. The interrupted label stays quiet. If our
accepted write then appears, recovery preserves the latest observable human
choice, including a change reported after the foreground wait ended. This changes
only the volume/mute baseline; the original content and pause/resume snapshot stay
separate. A later explicit remote digit relinquishes that recovery claim.

A late initial report can trigger one guarded recovery within the original
cleanup window: normally four seconds from the label's start, or five seconds for
a quiet cue. Autonomous recovery needs at least a full service acknowledgement
opportunity remaining. The initial write stays tracked until a native restore is
admitted or reliable feedback confirms settlement; scheduling a recovery task does
not release ownership. If its budget expires before admission, a fresh request can
initiate cleanup with a full recovery budget. No delayed autonomous write is
admitted after the original window. After pending provider/manual requests finish,
a fresh request at the reliable baseline can treat an expired missing write as
lost and try again. Physical commands cannot be recalled: a report that
arrives after a lost-write claim was released cannot be safely attributed or
corrected. An app/voice choice identical to an outstanding command's level is also
indistinguishable from that command's report.

A volume digit can keep the current audible setting before the label changes it,
or after a previous restore has already settled. If an earlier write is still
outstanding, each explicit digit can send its own level. A delayed mute report
from an older command cannot confirm a newer audible selection. If coalesced
commands produce no state change, a fresh label/cue request may release the manual
fence after its uncertainty window only when the reliable current level already
matches the audible selection and all provider/manual tasks have finished. The
window normally ends four seconds after the digit and never precedes an inherited
recovery deadline. A still-muted or different unresolved level remains guarded.
Relative volume buttons adjust the current audible level.

`sensor.remote_announcements` reports readiness, dependencies, clip duration,
service dispatch timing, `controller_elapsed_ms`, `controller_deadline_missed`,
and unfinished provider-call count. Failed recovery is explicitly
`recovery_unconfirmed`. `physical_latency_verified` stays false until audible
end-to-end measurement. Removing per-press TTS latency does not guarantee the
two-second physical target. AISIS has no speech-finished event; its completion is
reported as unconfirmed after clip duration plus a small buffer.

## Stock Mini: one-time Dialogs authorization

The existing YandexStation token returned HTTP 401 for Dialogs status. A scoped
Dialogs OAuth token and an owned **unpublished draft skill** are required. Create
or reuse the draft at https://dialogs.yandex.ru/developer/ and copy its ID.
A working conversation backend and publication are unnecessary for resources
used by YandexStation. On Odroid, run in a private interactive terminal:

```sh
python3 /home/ultra/homeassistant/scripts/provision_remote_announcements.py dialogs --skill-id OWNED_SKILL_UUID
```

The helper prints the official token link:
https://oauth.yandex.ru/authorize?client_id=c473ca268cd749d3a8371351a8f2bcbd&response_type=token

Token entry is hidden, with no token in shell history, chat or logs. The helper
checks access and skill ownership, writes mode-0600 ignored `providers.json`, then
calls `remote_announcements.prepare`. Add `--no-prepare` before component install.
Preparation reuses matching resources, uploads missing clips once and waits for
processing. Native playback uses the installed response-bearing
`yandex_station.send_command` service and the same `repeat_phrase` form payload
used by YandexStation's local text mode. The command contains only this verified
cached tag:

```text
<speaker audio="dialogs-upload/SKILL_UUID/SOUND_UUID.opus">
```

The service must exist and support responses before the controller changes volume
or pauses music. There is no alternate voice or generic-text fallback. A native
`SUCCESS` response confirms command acknowledgement, not audible playback or
physical latency; those require separate runtime evidence.

Ordinary audio URLs are excluded: installed YandexStation uses `audio_play`,
replaces selected media, and can replay the announcement when asked to resume.

## AISIS cache extension

`aisis_speaker.speak` already exposes `duid` and `text`. Quasar's
`/devices/:duid/push/text` uses its native TTS channel. The adapter patch inserts
a cached marker fast path before voice selection and synthesis, plus an
authenticated read-only capability endpoint. Ordinary TTS is unchanged. Unknown,
missing or corrupt markers fail with HTTP 503; no marker is sent to Fish/Alice.

The AISIS repository companion includes the adapter hook and
`services/yandex-station/code/announcement_cache.py`. Use its reviewed unit
deployment procedure with the matching files adjacent to one another, when no
AISIS conversation is active. The procedure serializes deployment and rollback,
preserves unknown maintenance ownership, and invalidates only the companion's
bytecode. Preserve any other work in a dirty checkout before composing the
reviewed adapter changes. After HA loads the component, import on Ubuntu:

```sh
python3 services/yandex-station/code/announcement_cache.py --source http://ODROID_ADDRESS:8123
```

This downloads only five checksummed clips to
`~/.cache/aisis/remote-announcements/`, without synthesis or playback. The importer
fully validates a new generation before switching one authoritative pointer;
failed writes preserve the previous playable generation. Previously advertised
queued markers can still use retained committed generations. Read
authenticated `GET http://127.0.0.1:31117/v1/remote-announcements/status` with the
existing adapter credential locally. Save its nonsecret ready response and run:

```sh
python3 /home/ultra/homeassistant/scripts/provision_remote_announcements.py aisis --receipt /path/to/status.json
```

The receipt must match all five HA clip keys. The explicitly selected speaker must
be connected. AISIS needs no stock Mini OAuth token.

## Rejected remote commands

`remote_announcements.reject` accepts a fixed `reason` and an optional logical
`press_id`. Remote guards pass the same identity through nested operations so a
single rejected command produces one cue. This service does not synthesize speech
or require an additional Dialogs upload or AISIS cache generation.

The selected announcement speaker receives its native short sound:

- Yandex uses the documented `alice-sounds-game-ping-1.opus` tag through the
  response-bearing native speech command. Its official public preview lasts about
  1.03 seconds.
- AISIS uses its existing `smart_speaker_play_iot_sound` directive with
  `isPowerOn: false`. Its physical duration has not been measured. The controller's
  1.25-second settle interval is a bounded wait, not a duration claim.

A cue temporarily lowers a valid nonzero speaker volume to
`min(current volume, 0.1)`, verifies the resulting HA state before sending audio,
and restores the prior level only while its ownership checks still hold. An
accepted quiet-level write remains tracked if its report misses the cue deadline;
a later report uses the same bounded recovery as a label. A human volume or mute
change wins. An unavailable, restored, muted or zero-volume speaker
is left alone and produces a nondelivery diagnostic. Native sounds preserve the
selected music queue; the cue sends no generic audio URL, pause, play or power
command. The speakers' firmware may briefly duck music on their native sound
channel.

Cues and existing spoken announcements share one playback lock through recovery.
The cue controller has a three-second attempt budget, including a 0.6-second
recovery reserve. Native acknowledgement must finish before the separately
reserved 1.25-second quiet completion interval. Even an ambiguous acknowledgement
failure retains that interval before restoration, unless an observed Yandex
speech-completion event proves the sound has ended. Cancelled provider calls remain
tracked by the existing service-call owner. Bounded shutdown retains the cue task
while its recovery settles.

Distinct rejections enter a FIFO with at most eight waiting cues. Identities are
deduplicated for 30 seconds with a bounded 256-entry history. Rejections waiting
more than three seconds before playback, or beyond the queue limit, are dropped
with an explicit reason. This age starts when the rejection is reported; an
operation's later confirmation failure is a new result, not an old queued cue.

`sensor.remote_announcements` exposes `rejection_status`, `rejection_reason`,
`rejection_drop_reason`, `rejection_recovery_reason` and cumulative
`rejection_accepted`, `rejection_sent`, `rejection_dropped`, and
`rejection_duplicates` counters. Each update clears inapplicable reason and timing
fields, so a later successful cue does not retain an earlier failure. Cue timing
uses `rejection_controller_elapsed_ms`, `rejection_controller_budget_seconds`,
`rejection_controller_deadline_missed`, and `rejection_pending_service_count`.
Failed cue recovery remains `recovery_unconfirmed` with its safe recovery reason,
including after a delivery failure. That delivery failure retains its
`rejection_drop_reason` and increments the dropped counter, not the sent counter.
Shutdown uses the explicit drop reason `closed`. Ordinary cue reporting stays
separate from spoken-label readiness and controller diagnostics. A failure in the
shared late-volume recovery may update the main controller status; that diagnostic
does not select a target or grant dispatch permission. Shared recovery ownership
and guards still prevent another unsafe attempt.

A queued service response does not prove playback. `completed` means a native
Yandex completion event was observed; `sent_unconfirmed` means dispatch completed
without that event. Neither
is acoustic proof of audibility. Physical cue level, duration and continuity of
ongoing music require separate acceptance evidence.

These cue rules preserve the existing spoken-label texts and cache identities.

## Silent preparation and checks

```sh
python3 scripts/prepare_remote_announcements.py
python -m unittest discover -s tests -p 'test_remote_announcements*.py'
```

Preparation uses the installed HA TTS and plays no speaker audio. Scripts create
private `/tmp` logs and print their paths. Tests are bounded and isolated; full
repository suites belong in CI or RunPod under the host policy.

Primary references:

- https://yandex.ru/dev/dialogs/alice/doc/ru/sounds/games
- https://github.com/AlexxIT/YandexStation#text-to-speech
- https://yandex.ru/dev/dialogs/alice/doc/ru/resource-sounds-upload
- https://www.home-assistant.io/integrations/tts/
- https://github.com/v3rt3p/quasar-server/blob/main/src/routers/uniproxy/uniproxy-connection.ts
- https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech
