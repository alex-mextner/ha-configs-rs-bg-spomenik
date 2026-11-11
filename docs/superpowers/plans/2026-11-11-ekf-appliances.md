# EKF Appliances Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate projector and complete Haier climate devices through the local EKF remote.

**Architecture:** Original HA config-entry integration with pure wire encoders, serialized transport, and climate/media-player/button platforms. Existing projector scripts preserve power/Kodi/source behavior.

**Tech Stack:** Python 3.14, Home Assistant 2026.9.3, stdlib unittest.

**Spec:** `docs/superpowers/specs/2026-11-11-ekf-appliances-design.md`

## Global Constraints

- No deployment, restart, registry mutation, or appliance commands.
- No copied third-party integration code; no new dependencies.
- Six Haier single frames, 150ms apart, one shared transmission lock.
- Preserve full climate settings and all twelve verified projector commands.
- Setup/restoration never send; unknown sensor readings are not invented.

## Review Focus

- A mid-burst failure must not commit a climate state that was not fully sent.
- Concurrent changes must preserve both settings and serialize complete bursts.
- Restored malformed values must not enable the AC or fabricate a temperature.
- Projector power scripts preserve helper/Kodi behavior without lock deadlocks.
- Invalid selectors, missing entities, and unavailable helpers fail without transmission.

---

### Task 1: Wire formats and serialized transport

**Files:** Create `custom_components/ekf_appliances/protocol.py`, `transport.py`; tests `tests/ekf_appliances/test_protocol.py`, `test_transport.py`.

**Interfaces:** `haier_state(mode, temperature, fan, swing, preset) -> bytes`; `haier_pulses(state) -> tuple[int, ...]`; `projector_pulses(command) -> tuple[int, ...]`; `encode_pulses(pulses) -> str`; `Transport(send: async callback).async_send(pulses, repeats=1, gap=0) -> None`.

- [ ] Write literal fixtures: off ending 05 5D; cool/24/auto/off/none = A6 80 00 00 40 A0 00 20 00 00 00 00 08 2E; Haier frame 230 timings; NEC power data 00 BD 01 FE LSB-first; LE16 encoding; invalid mode/range tests.
- [ ] Run stdlib unittest and verify missing functionality fails.
- [ ] Implement independent encoders and transport; serialize whole bursts and propagate errors.
- [ ] Verify exact six sends/five gaps, no interleaving, and pure protocol suite passing.
- [ ] Commit protocol/transport and tests.

### Task 2: HA devices and configuration

**Files:** Create `__init__.py`, `const.py`, `config_flow.py`, `entity.py`, `climate.py`, `media_player.py`, `button.py`, `manifest.json`, `strings.json`, `translations/ru.json`; test `tests/ekf_appliances/test_integration.py`.

**Interfaces:** Config keys from spec; `entry.runtime_data` supplies shared transport/device identifiers. Platforms add one climate, one player, twelve buttons with stable unique IDs.

- [ ] Write HA behavior tests for startup silence, failed-send rollback, off-setting changes, restore, invalid values, real sensor subscription, helper availability, power-script routing, and button command map.
- [ ] Run in the isolated HA image; verify failures before implementation.
- [ ] Implement selectors, validation, device identity, restoration, and platforms using installed HA APIs.
- [ ] Run complete focused suite and confirm payload/state behavior.
- [ ] Commit original HA implementation and tests.

### Task 3: Review and handoff

**Files:** Create integration `README.md`; update completion checkboxes and verification ledger.

- [ ] Document selectors, identities, migration, assumed-state limits, sources, and exact test command.
- [ ] Run complete focused suite, JSON validation, compile checks, and `git diff --check`.
- [ ] Request independent whole-branch review; fix material findings RED→GREEN.
- [ ] Commit and return branch/commits/tests/flow/identity/migration details without deploying.
