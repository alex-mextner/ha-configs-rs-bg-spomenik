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

Speech volume is `max(current volume, configured volume_floor)`, currently 0.4
in this package (HA 0..1; speaker 0..10). Restore the old volume only while the
current level still equals our temporary level and the
speaker was not muted by the user during speech. Yandex volume-zero mute is lifted
for speech and its remembered volume plus previous mute state are restored; user
changes during speech win. Installed AISIS cannot restore zero/mute (its volume API
accepts 1..10 and has no mute operation), so that case reports
`aisis_cannot_restore_muted_or_zero_volume` explicitly. No Alice fallback is used.

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

## Silent preparation and checks

```sh
python3 scripts/prepare_remote_announcements.py
python -m unittest discover -s tests -p 'test_remote_announcements*.py'
```

Preparation uses the installed HA TTS and plays no speaker audio. Scripts create
private `/tmp` logs and print their paths. Tests are bounded and isolated; full
repository suites belong in CI or RunPod under the host policy.

Primary references:

- https://github.com/AlexxIT/YandexStation#text-to-speech
- https://yandex.ru/dev/dialogs/alice/doc/ru/resource-sounds-upload
- https://www.home-assistant.io/integrations/tts/
- https://github.com/v3rt3p/quasar-server/blob/main/src/routers/uniproxy/uniproxy-connection.ts
- https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech
