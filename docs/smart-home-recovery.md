# Smart-home automatic repair

The user service on ubuntu-pc consumes live Home Assistant and Music Assistant events from Odroid. It keeps credentials on Odroid and uses the shared `agent-sub` subscriber lifecycle to reach a repair agent. There is no polling timer, periodic health scan or automatic service restart. The existing plant detector keeps its sensor thresholds and deterministic recovery sequence, while its last-resort agent delivery uses the same shared lifecycle under `plant-sensors`.

## Monitored failures

The source maintains authenticated WebSocket subscriptions to both local APIs and a new-lines-only Docker log stream. Music Assistant snapshots seed a baseline after connection; a startup or reconnect snapshot cannot manufacture a playback interruption. The log stream starts with `--tail 0`, so historical errors cannot open incidents.

Coverage is declarative:

- Core HA and MA connection failure, including source or SSH transport loss. A monitoring connection failure does not establish that a speaker stopped playing.
- Current errors from every enabled Music Assistant provider. Deliberately disabled providers and error-free providers still starting up are ignored.
- Enabled HA entries in `integration_domains`: initially `music_assistant`, `yandex_station`, `yandex_smart_home`, `ekf_appliances` and `aisis_speaker`. Setup errors and retry states are reported; unrelated integrations and powered-off television entities are not scanned.
- Actual interruptions on the physical speakers listed in `players`. A scoped stream error must match the current item URI or an exact configured transport alias locally. Only the sanitized reason and endpoint identity leave Odroid.

There is no generic alert for arbitrary unavailable entities, old Kodi entries, idle players, background audio analysis, or an unrelated historical log error. Extra critical integrations and speakers can be added explicitly to the configuration.

## Playback and cause

| Evidence | Agent behavior | Spoken message |
| --- | --- | --- |
| Network interruption shorter than five seconds | Observe MA's natural reconnect; no agent launch | None |
| Network failure still present after five seconds | Notify the existing subscriber, or launch and subscribe one agent | `Подключаюсь к интернету.` only if the affected ongoing playback remains interrupted |
| Confirmed authentication or configuration fault | Dispatch immediately | `Ошибка подключения к музыкальному сервису. Исправляю, минутку.` only on an actually interrupted endpoint |
| Code or unclassified service fault | Dispatch immediately without pretending the cause is network or configuration | `Сбой музыкального сервиса. Исправляю, минутку.` only on an actually interrupted endpoint |
| Monitoring transport failure | Reconnect with bounded backoff; report persistent monitoring failure | None without independent playback-failure evidence |

Pause, stop, next, source changes and normal track endings are not fault evidence. The pinned MA intent hook runs after authentication and parsed input at both HTTP and WebSocket dispatch. Permission-aware target validation produces a private pending record before effects; only a successful handler produces an accepted record. Rejected, malformed and unauthorized commands do not produce accepted telemetry. These records are server logs, never events broadcast to guests or restricted users. Their payload contains only command, target, time and correlation, without media URLs or arguments.

HA service-call events provide the corresponding explicit control intent for HA routes. A control intent or changed queue/item cancels a pending announcement. Group queue IDs stay separate from physical speaker IDs. Protocol aliases map to one physical endpoint, so a universal player and its AirPlay child cannot produce duplicate speech.

Every event carries its source boot ID, stream ID, sequence, original time and monotonic timestamp. Delayed, missing or reordered evidence cannot turn a reconnect baseline into an interruption. Later authentication or code evidence upgrades a pending network cause immediately. A real device failure near the end of a track remains a failure; normal idle transitions without fault evidence remain quiet.

## Announcements

Speech is requested only for the specific speaker whose known ongoing playback failed. The controller persists a claim before the request, permits one attempt per incident and endpoint. A genuinely new interrupted playback is eligible immediately; there is no blanket endpoint cooldown. An ambiguous response is not retried. Speech runs independently of agent startup; neither failed speech nor a slow agent blocks event intake or cancellation.

At the deadline, a one-shot local check verifies the same queue and current item, absence of newer user intent, unrecovered playback, current reachability and mute. A response arriving more than three seconds after its speech deadline is suppressed. This narrowly reactive path relies on the affected speaker's known ongoing playback; it has no invented wall-clock or person-home gate. Separate proactive watering-reminder policies are unchanged.

Cached Ogg Opus clips are served from HA `/local/smart-home-recovery/` and verified by SHA256 before use. The network phrase therefore does not need cloud synthesis during a WAN outage, provided MA, HA's local HTTP service and the speaker's LAN route remain reachable. No other room is used as a fallback. A complete failure of those local audio components is repaired silently.

The supported MA `players/cmd/play_announcement` API supplies announcement and volume restoration semantics. The announcement request does not pause, clear or rewrite a music queue. Emulated announcements are allowed only for an already interrupted standalone endpoint; the captured state is idle, so restoration must not start a queue. Group fan-out and ambiguous grouping are suppressed. Native regressions verify these installed MA semantics without physical playback.

The request uses the current Music Assistant control volume, subject to its existing configured announcement limits, and native MA restoration retains the saved level. Muted speakers stay quiet. For the studio, each concrete attempt reads the exact connected Quasar device `M00ZRW3008RAJG` through the existing local read-only state endpoint; only a matching identity and boolean `SoundMuted: false` admit audio. The separate Quasar `SoundLevel` scale is never converted into an AirPlay percentage. Other devices use their own endpoint or actual active protocol. Unknown mute stays quiet; a stored nonzero volume is insufficient. Actual hardware volume preservation remains part of production verification; no audible acceptance test is implied by source-level restoration tests.

Own announcement requests have a bounded private marker before submission. Their temporary playback transitions are excluded from recovery and new-failure detection. Recovery is established by fresh service or playback evidence, never by the announcement becoming `playing` or by an agent's written report. After an actual endpoint/provider reconnect, a still-idle original queue may receive one bounded resume attempt. It requires the same queue and item, a current unmuted standalone endpoint, and no subsequent user command. A pause, Stop, queue/source change or stale epoch cancels it. The request runs after any own announcement and carries a private correlation marker so it is not misclassified as user intent. Its accepted receipt is not proof that playback resumed.

## Repair lifecycle

HA owns incident identities, immutable event envelopes and speech claims. **The shared agent-tools broker alone owns subscriptions and agent launches.** `agent-sub publish --source smart-home --event-file PATH --launch-profile smart-home` delivers to the existing verified native subscriber. With no holder, the broker reserves the native source, starts an idle OMP endpoint, binds it atomically, and only then delivers the event. No incident-specific prompt runs before binding.

`delivered` means native input acceptance, not completed repair. `launched` and `pending` require broker reconciliation. A recovered event with no subscriber is recorded without launching an agent. Identical event IDs and contents are safe to retry after an ambiguous response; changed content has a new event ID. The consumer reconciles once at startup, then schedules one-shot deadlines only while concrete delivery work is pending. A receipt hands retry ownership to the shared spool only when `durable:true`, source and event ID match and its immutable-envelope digest is valid. The native endpoint `receipt_id` may still be null during launch. An ambiguous or mismatched response leaves the producer's immutable publication pending.

The shared watch also reports authoritative startup vacancy. Only a positive `vacant` snapshot with its authority and revision can trigger a fresh reconciliation of previously published open work. Unknown ownership does not imply absence. A new domain revision is issued only if current service evidence still shows the fault; a synthetic startup observation is never acknowledged as a native lifecycle event.

The consumer also subscribes to `agent-sub watch`. A positively confirmed native subscriber death triggers one fresh API reconciliation of still-open domain work, then a new immutable event revision. Unknown liveness never creates a replacement. A repaired domain closes without launching a new agent.

Durable state survives restarts. Invalid state is preserved separately and rebuilt; the shared native subscription still prevents competing agents. Speech claims are never replayed from a previous source epoch. The standing repair brief requires fresh evidence, preserves user stops and queues, and follows normal repository tests, review and shipping gates. It forbids manual edits of HA `.storage` or runtime databases and forbids unsolicited household audio or people-directed messages.

The existing Odroid `/home/ultra/.ma-maintenance.lock` uses exclusive creation with an owner PID and nonce. A current or unknown owner suppresses ordinary detection during scoped maintenance; a positively absent or reused owner PID is classified stale without deleting its file. Linux file events notice its creation/removal; removal triggers one fresh MA baseline. A lock persisting for fifteen minutes produces a silent maintenance diagnostic instead of hiding a stuck maintenance operation forever. The repair agent must reconcile the lock owner before changing a service.

## Files and controls

On ubuntu-pc:

- Install the reviewed `ha_smart_home_common.py`, `ha_smart_home_recovery.py` and `ha_smart_home_service.py` under `~/.local/share/ha-smart-home-recovery/`.
- Install the shared agent-tools `agent-sub` command and the owned mode-0600 profile at `~/.config/agent-tools/subscriptions/profiles/smart-home.json`. Use the example profile with the reviewed standing instructions file; no event-file launch argument is allowed.
- State and immutable delivery evidence live under `~/.local/state/ha-smart-home-recovery/`.
- Install and enable `ha-smart-home-recovery.service` as a user service. User lingering must be enabled for boot startup. The superseded `ha-smart-home-recovery.timer` must remain absent or disabled.
- Preserve its explicit `OnFailure=service-failover-repair@ha-smart-home-recovery.service.service` and `RestartMode=normal`. The installed shared service-failover template handles the controller's own failures; repair/watch units themselves must remain excluded from recursive failover.

On Odroid:

- Install `ha_smart_home_common.py`, `ha_smart_home_recovery.py`, `ha_smart_home_events.py`, `ha_smart_home_watch.py` and `ha_smart_home_probe.py` under `~/.local/share/ha-smart-home-recovery/`.
- Create the small isolated `venv` there and install the SHA256-pinned pure Python `websockets` wheel from `ha-smart-home-events-requirements.txt`. It supports the host's Python 3.13 without a native build.
- Install the reviewed configuration at `~/.config/ha-smart-home-recovery/config.json`, and the verified clips under `/home/ultra/homeassistant/www/smart-home-recovery/`.
- Apply the checksum-guarded reconnect and intent startup wrapper only through the reviewed scoped MA deployment procedure. It validates complete original/output hashes before writes and refuses an unexpected image. There is no package download or database mutation at startup.

The SSH source terminates with its parent channel. Source lifecycle, accepted intent and current announcement markers stay under Odroid `~/.local/state/ha-smart-home-events/`. Private rotating logs under `/tmp/ha-smart-home-events-<uid>/` retain two days of this service's own run logs. Credentials, raw service bodies and secret-bearing source URLs are never logged.

```sh
systemctl --user status ha-smart-home-recovery.service
systemctl --user is-enabled ha-smart-home-recovery.service
systemctl --user is-enabled ha-smart-home-recovery.timer
```

Stopping the event service pauses new automatic work. Existing subscribed repair sessions remain owned by the shared broker and are not silently killed by that action.

## Verification

The `tests/test_smart_home_*.py` modules cover event order, short reconnects, cause upgrades, user controls, natural endings, aliases, groups, stale data, own announcements, mute and deadline rules, cached clip routing, maintenance file events and nonblocking agent delivery. They are included by the maintained CI target. Native pinned-image tests under `tests/music_assistant_reconnect/` exercise reconnect cleanup, complete-source guards, real HTTP/WS dispatch and native announcement restoration without devices.

Acceptance requires an actual authenticated source snapshot plus controlled no-audio event replay into the shared subscription lifecycle: one native agent must receive and respond to a problem, a second problem must reach the same subscribed agent, and a stale subscriber must be reconciled safely. A mocked process start, old polling proof or prior global speaker announcement does not accept this event-based design. Production activation follows the verified merged artifact; no physical outage or unsolicited audible test is needed.
