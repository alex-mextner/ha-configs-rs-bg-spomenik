# Smart-home automatic repair

The user service on ubuntu-pc checks Home Assistant and Music Assistant on Odroid every minute. It runs independently of both applications, and keeps their existing credentials on Odroid. A persistent fault starts one native OMP repair agent through the installed wrapper; it does not depend on Guardian.

## Monitored failures

The probe checks the authenticated Home Assistant API, Music Assistant API, and the enabled `hass` and `hass_players` providers. Missing expected provider configurations, an unloaded provider, or a provider error remain failures. Intentionally disabled providers are ignored. Player power state, old unavailable Kodi duplicates, and old logs are not fault signals.

Selected Home Assistant integration domains are also checked: `music_assistant`, `yandex_station`, `yandex_smart_home`, `ekf_appliances`, and `aisis_speaker`. Enabled entries must be loaded. Unknown payloads and inaccessible probes become monitoring incidents instead of reporting a healthy home. Other integrations can be included explicitly through the probe configuration.

The existing plant sensor recovery controller stays independent and unchanged.

## Repair lifecycle

A fault must persist for 120 seconds. Correlated faults share one durable incident. For a Home Assistant provider fault, the controller requests one supported provider reload and waits another minute. An unresolved problem starts one bounded OMP service, with a persisted launch claim and service reconciliation before every retry.

The service has a 25-minute lifetime limit; OMP has a 20-minute turn limit. A failed or exited repair can resume its saved session after a backoff of five minutes, increasing to one hour. An ambiguous launch never creates a second worker until systemd establishes that the first is absent. A previous incident's active worker prevents a new worker from starting. All agents use the usual review and deployment rules and must recheck the active incident before a mutation.

A repair report does not close an incident. Closure requires two distinct, fresh successful probes at least 20 seconds apart. State survives monitor restarts and reboot. Invalid monitor state is preserved separately and rebuilt; systemd worker reconciliation still prevents overlapping launches.

## Spoken notice

After a repair has been scheduled, the monitor requests: “Умный дом настраивается, минутку.” It persists the attempt before sending, so an unknown delivery result is not repeated. There is at most one attempt per incident and a global 30-minute cooldown.

The normal route is the living-room Yandex Station's supported native text announcement. It uses the existing YandexStation temporary-volume mechanism: 6/10 while a configured room player is playing, 4/10 otherwise, then the integration restores its previous level. The monitor does not replace a music queue or issue play/pause commands.

Sound is suppressed when nobody is confirmed home, presence is unknown, the speaker is muted or unavailable, or during quiet hours (default **22:00–09:00 Europe/Belgrade**). An unchanged current `home` state is valid; the five-minute cache age limit applies only when HA cannot provide a current observation.

When Home Assistant is unavailable, the monitor can use the existing Quasar API only if the exact living-room Station is connected there, cached occupancy is under five minutes old, and the direct speaker is idle and at a quiet nonzero volume. It never substitutes a speaker in another room. In the current installation only the studio Station is connected to Quasar, so a complete HA outage is repaired silently until a supported living-room route becomes available. Speech failure or suppression never prevents repair.

## Files and controls

On ubuntu-pc:

- Installed scripts: `~/.local/share/ha-smart-home-recovery/`.
- State and incident evidence: `~/.local/state/ha-smart-home-recovery/`.
- Timer: `ha-smart-home-recovery.timer`; user lingering is required for boot startup.
- Each repair has a systemd service named `ha-smart-home-repair-<incident>.service`.

On Odroid, install `ha_smart_home_probe.py` at `~/.local/share/ha-smart-home-recovery/`. Optional configuration is `~/.config/ha-smart-home-recovery/config.json`, with `speaker`, `persons`, `playback_entities`, `integration_domains`, `quiet_start`, `quiet_end`, and `timezone`. It contains entity identifiers and policy only; credentials remain in their existing local stores.

Inspect current evidence with:

```sh
python3 ~/.local/share/ha-smart-home-recovery/ha_smart_home_recovery.py status
systemctl --user status ha-smart-home-recovery.timer
```

To pause new automated work, create the owned marker `~/.local/state/ha-smart-home-recovery/paused`; remove that marker to resume. To disable periodic checks, disable the timer. Existing repair processes are visible as separate systemd units and are not silently killed by a pause marker.

Both scripts print progress and a unique private log path to stderr, with machine-readable JSON on stdout. Their own `/tmp/ha-smart-home-*` run logs expire after two days. No token, API response body, or credential file is logged.

## Verification

`tests/test_smart_home_recovery.py` is a standard-library regression suite included by the existing maintained test target. It checks persistence, debounce, deduplication, timeout ambiguity, dead-worker retries, single provider reload, malformed/stale health, recovery confirmation, volume policy, quiet hours and presence suppression.

End-to-end dispatch verification uses an isolated state directory and a read-only brief. It must observe an actual native agent response and terminal process receipt, as well as a second monitor tick that does not create another worker. No production service needs to be broken for this check. Record accepted audio delivery separately from a person's confirmation that sound was heard.
