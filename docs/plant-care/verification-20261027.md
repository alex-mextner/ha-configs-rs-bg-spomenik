# Plant Care verification — 2026-10-27

## Deployed behavior

The existing Home overview has the auto-discovering plant card; no plant sidebar
was added. Adaptive Plant 1.2.3 is pinned at
`87af7d212f69986f7cc0dda4faece7b2abb3a47c` as a Git submodule.
Plant Care Alerts, the card and the delivery blueprint are MIT-licensed.

The real Strelitzia uses the existing soil sensor. Calibration stays dry ADC 67,
wet ADC 221. Initial warning threshold is 25% of this relative scale, recovery
35%; these values are not botanical measurements of volumetric water content.
Orange requires six hours of confirmed dryness. Red starts 24 hours after
orange. Hourly living-room announcements are enabled from 08:00 until 22:00.
No automatic watering actuator is controlled.

Manual watering and a 24-hour snooze are exposed on the card. Native Adaptive
Plant settings provide photo upload and addition of other plants. No private
plant photo was supplied; a neutral sprout placeholder is shown.

## Automated verification

The scoped suite ran in an isolated directory inside the installed Home
Assistant 2026.9.3 container, without modifying real entities:

- 41 unittest tests passed, with no skipped tests.
- 10 JavaScript card assertions passed; JavaScript syntax passed.
- Scoped Ruff lint and format checks passed.
- Rig cognitive/cyclomatic complexity checks passed after extracting source
  reading, per-plant refresh, publication and removal helpers.
- The full live Home Assistant configuration passed `check_config` before the
  supported safe-reload script was used.

Regression tests first reproduced the phone notification race: a sensor could
recover while clearing its old notification, leaving an acknowledged warning
that no longer existed on the phone. Both the helper and the actual registered
service callback now re-arm delivery after a completed clear. Startup events
wait until automation listeners are ready.

## Live evidence

A read-only Recorder query at 2026-10-27 19:13 UTC after safe reload showed:

- `sensor.strelitsiia_poliv`: `ok`, relative moisture 70.2%, seven samples;
- raw ADC 205, sampled at 19:11:16 UTC;
- calibration unchanged at 67/221;
- voice switch and plant notification automation both `on`;
- last marked watering still 2026-10-26.

Earlier authenticated UI checks on the same day captured real desktop/mobile
Home views and the upstream image-upload settings. Orange/red screenshot
examples are explicitly labelled read-only previews; they do not change the
real plant's measurements, timers or watering history. Screenshot percentages
are from their capture time, not a later measurement.

The earlier labelled phone test completed at the HA service layer. The living-
room TTS test completed and station telemetry changed and returned to idle.
These observations do not independently prove physical audibility or delivery
to an unlocked phone screen. No live plant was marked watered for testing.

## Remaining repository-wide validation debt

The full repository pytest command was attempted: 54 collection errors in
unrelated `entity_note` / `home_generative_agent` tests, including missing
`awesomeversion`, `langchain_ollama` and the HGA source/environment. This is not
a green full-repository test run.

Existing aggregate CI also reports broad baseline lint and `chatgpt_native`
manifest/HACS metadata failures. The Plant Care manifest ordering error was
fixed in the validation commit, with a regression test. The dedicated Plant Care workflow
checks the scoped implementation against the deployed HA version without
suppressing any aggregate checks.

## Review resolutions

The independent review's startup-order concern was checked against the actual
installed HA source. In HA 2026.9.3, automation triggers attach via a startup job
before `EVENT_HOMEASSISTANT_STARTED` (`components/automation/__init__.py`, lines
858–862). The companion uses `async_at_started`, so the proposed lost-initial-
event race does not apply to that version. The CI image stays pinned to it.

A proposed late-clear/after-replacement duplicate requires concurrent runs of
the same delivery automation. The shipped blueprint uses `mode: queued`, so its
clear and acknowledgement precede the next warning's delivery. Do not create
multiple delivery automations for the same plants.

Deferred minor behavior: a refresh before STARTED can increment retry backoff
before listeners are ready, although STARTED republishes the current state;
repeated real sensor dropouts can clear and restore a phone warning repeatedly.
Restoring a genuine warning is preferred to silently losing it. These are not
infinite delivery loops. No delays, quiet-hour settings or measurements were
changed to conceal them.
