# Strelitzia and Alice

`packages/plant_care_yandex.yaml` publishes the existing care engine as
`sensor.strelitzia_yandex_moisture` and `script.plant_care_status`.
The sensor's Russian name is **Стрелиция**. Its percentage is a relative index
between the probe's dry and wet calibration points, not a volumetric water
measurement. Values beyond the wet calibration point are clipped to 100%; this
alone does not establish the physical condition of the soil.

The report uses the same smoothed moisture and care state as the existing engine.
Both the raw ADC measurement and the care engine's own sample must be at most
20 minutes old and must not be in the future. Missing readings, invalid
calibration and invalid values produce an unavailable sensor, never a false 0%.
Prolonged high readings take priority over a watering suggestion. A date is
reported only when the engine has a valid future drying forecast; the calendar
`next_watering` field is not used as a substitute.

## Installation and existing runtime settings

This repository uses Home Assistant packages. Keep the existing
`homeassistant.packages` directory inclusion and initialize the pinned dependency:

```sh
git submodule update --init submodules/adaptive-plant
```

`custom_components/adaptive_plant` links to the MIT Adaptive Plant source at
`87af7d212f69986f7cc0dda4faece7b2abb3a47c`.
The accompanying `plant_care_alerts` integration, packages and notification
blueprint are included here so the Alice package has its required source chain.
The companion also exposes its existing photo upload service, which depends on
Home Assistant's `file_upload` integration and Pillow.

The soil package requires the host bridge at `http://127.0.0.1:8768`.
Its source and systemd unit are in `scripts/soil_moisture/`. On the configured
Odroid host it is installed at `/opt/ha-soil-moisture/soil_bridge.py` and requires
Python 3, the host's `python3-libgpiod` package with the v2 `request_lines` API,
the matching GPIO chip/line and ADC sysfs path, and the unit's GPIO access.
Review these hardware-specific defaults before installing on another machine.
The unit and source are supplied for reproducibility; this change does not
restart or reconfigure the live bridge. Each invocation writes bounded private
lifecycle/request logs under `/tmp`, with the path printed on startup. Requests
and failures do not log raw request paths, sampled values or exception details.

Adaptive Plant instances, calibration helper values, the selected reminder
speaker, Yandex Smart Home exposure and Alice scenarios are runtime settings.
They are not recreated by copying this repository. Preserve them on the existing
installation. For a new installation:

1. Add Adaptive Plant through its native integration UI, select
   `sensor.strelitzia_soil_smoothed`, and set the plant's own care preferences.
   Confirm its companion entity is `sensor.strelitsiia_poliv`, or update the
   Alice package to the actual entity ID.
2. Calibrate the actual probe using the soil package's dry/wet controls. The
   example installation's stored values are not portable to a different probe.
3. Add **Plant Care Alerts** through Settings → Devices & services and select a
   supported Yandex Station or Aisis notification entity. That setting controls
   speech; the Alice script contains no fixed speaker ID.
4. Configure the existing notification blueprint's mobile notification target
   for the installation. The package currently names the original household's
   target. Existing automatic reminders retain their own enable switch and quiet
   hours; this package adds no announcement schedule.
5. In the existing Yandex Smart Home integration's options, append
   `sensor.strelitzia_yandex_moisture` and `script.plant_care_status` to the
   selected entities, preserving other selections. Refresh devices in Yandex.
   Create an Alice scenario with the desired voice phrases, such as
   **«состояние растений»** and **«когда поливать стрелицию»**, that invokes the
   exposed script. The sensor's moisture class maps to the supported percentage
   humidity property in Yandex.

Use native configuration flows and services. Do not copy or edit `.storage`.
On an existing installation, validate YAML and use template/script reloads when
only the Alice package changes. A first installation of the companion and
Adaptive Plant needs the usual Home Assistant integration setup/restart.

## Verification and operation

A report can be inspected without speech by calling `script.plant_care_status`
with `speak: false` and requesting its service response. The default is to speak
once through `plant_care_alerts.announce`. The refresh action can still update
normal care transitions and existing pending notification delivery; `speak:
false` controls this script's audio, not all effects of the existing care engine.

The deployed package was checked in Home Assistant 2026.9.3 using 13 native
freshness/calibration cases, nine message branches, targeted domain reloads and
a live silent report. Its current example was:

> Стрелиция: индекс влажности 100%. Показания долго высокие. Проверь грунт перед поливом.

Yandex device inventory and scenario configuration were read back successfully.
No audible end-to-end voice request was performed. The package has SHA256
`7ac0179cb483f28d660d98cf481552c1a62db0cb1c635190d7923210175dec93` and Git blob
`db9558f0edf195436542038060220499a7cc5684`.

The **Plant care and Alice status** CI job runs the native HA script and template
regressions, source inference/state tests, speaker transport tests and the
bridge's simulated hardware/HTTP tests. CI uses the same Home Assistant version
as the deployment. It makes no real hardware or speaker calls. Existing Ruff,
Hassfest, HACS and maintained repository regression jobs remain in force.

This source publication draws on the earlier implementation in PR #35, while
starting from current remote `main` and excluding unrelated production history.
