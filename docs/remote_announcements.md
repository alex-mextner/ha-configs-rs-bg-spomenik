# Universal remote announcements

The package follows confirmed changes of `sensor.universal_remote_target`:
`chromecast`, `tv`, `kodi`, `projector`, `speaker`. Startup, unavailable states and
repeated targets are quiet. Pending rapid changes coalesce; an active announcement
gets its volume/queue recovery first.

The exact installed voice is **Брачо**, Fish reference
`5cd1ffa5dbb4419cb5766435139c6d92`, entity `tts.bracho_bracho`. Missing or corrupt
clips are generated at preparation/startup, then converted once to mono 48 kHz
Ogg Opus at tempo 1.25. Route changes never synthesize or upload. Runtime audio and
private provider settings live in `.tts/remote_announcements/`, already ignored
by Git. Audio binaries must not be committed.

The current living-room Mini is `media_player.yandex_station_m0011hm00z86hg`.
`input_select.remote_announcement_backend` selects `Яндекс` or `AISIS`, initially
`Яндекс`. This selector is independent of the music remote's backend.
Explicit selection addresses that configured speaker regardless of HA room. The
configured AISIS Mini `media_player.aisis_speaker_m00zrw3008rajg` is in the studio;
selecting AISIS intentionally addresses it. Room `gostinaia` is only a filter for
automatic fallback. Update the entity setting when the physical speaker changes.

## Music, volume and latency

Both providers use the native speech channel and preserve the selected music
queue. Firmware controls music attenuation; a ducking level is not yet measured.
`music_mode: pause` is the explicit fallback: pause known playing content, speak,
then resume only unchanged original content if still paused.
In pause mode, waits for speech completion share a two-second budget from the
start of dispatch; missing completion signals cannot add a further five seconds.
Service transport and recovery calls can themselves exceed that budget, so it is
not a guarantee of physical pause length or audible end-to-end latency.

Speech volume is `max(current volume, 0.5)` (HA 0..1; speaker 0..10). Restore the
old volume only while the current level still equals our temporary level and the
speaker was not muted by the user during speech. Yandex volume-zero mute is lifted
for speech and its remembered volume plus previous mute state are restored; user
changes during speech win. Installed AISIS cannot restore zero/mute (its volume API
accepts 1..10 and has no mute operation), so that case reports
`aisis_cannot_restore_muted_or_zero_volume` explicitly. No Alice fallback is used.

`sensor.remote_announcements` reports readiness, dependencies, clip duration and
service dispatch timing. `physical_latency_verified` stays false until audible
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
processing. Native playback uses `media_player.play_media`, type `text`:

```text
<speaker audio="dialogs-upload/SKILL_UUID/SOUND_UUID.opus">
```

Ordinary audio URLs are excluded: installed YandexStation uses `audio_play`,
replaces selected media, and can replay the announcement when asked to resume.

## AISIS cache extension

`aisis_speaker.speak` already exposes `duid` and `text`. Quasar's
`/devices/:duid/push/text` uses its native TTS channel. The adapter patch inserts
a cached marker fast path before voice selection and synthesis, plus an
authenticated read-only capability endpoint. Ordinary TTS is unchanged. Unknown,
missing or corrupt markers fail with HTTP 503; no marker is sent to Fish/Alice.

In the AISIS repository add `services/yandex-station/code/announcement_cache.py`
and apply `adapter.patch`. Deploy through the normal service procedure when no
AISIS conversation is active. After HA loads the component, import on Ubuntu:

```sh
python3 services/yandex-station/code/announcement_cache.py --source http://ODROID_ADDRESS:8123
```

This downloads only five checksummed clips to
`~/.cache/aisis/remote-announcements/`, without synthesis or playback. Read
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
