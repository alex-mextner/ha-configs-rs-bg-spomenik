# EKF Appliances Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Separate projector and complete Haier climate devices through the local EKF remote.

**Architecture:** Original HA config-entry integration with pure wire encoders, serialized transport, and climate/media-player/button platforms. Existing projector scripts preserve power/Kodi/source behavior.

**Tech Stack:** Python 3.14, Home Assistant 2026.9.3, stdlib unittest.

**Spec:** `docs/superpowers/specs/2026-11-11-ekf-appliances-design.md`

## Global Constraints

- No deployment, restart, registry mutation, or appliance commands.
- No copied third-party integration code; no new dependencies.
- One Haier frame per action, with one shared transmission lock.
- Preserve full climate settings and all twelve verified projector commands.
- Setup/restoration never send; unknown sensor readings are not invented.

## Review Focus

- A transmission failure must not commit a climate state that was not fully sent.
- Concurrent changes must preserve both settings and serialize complete bursts.
- Restored malformed values must not enable the AC or fabricate a temperature.
- Projector power scripts preserve helper/Kodi behavior without lock deadlocks.
- Invalid selectors, missing entities, and unavailable helpers fail without transmission.

---

### Task 1: Wire formats and serialized transport

**Files:** Create `custom_components/ekf_appliances/protocol.py`, `transport.py`; tests `tests/ekf_appliances/test_protocol.py`, `test_transport.py`.

**Interfaces:** `haier_state(mode, temperature, fan, swing, preset) -> bytes`; `haier_pulses(state) -> tuple[int, ...]`; `projector_pulses(command) -> tuple[int, ...]`; `encode_pulses(pulses) -> str`; `Transport(send: async callback).async_send(pulses, repeats=1, gap=0) -> None`.

- [x] Write literal fixtures: off with Health disabled ending 05 5B; cool/24/auto/off/none = A6 80 00 00 40 A0 00 20 00 00 00 00 05 2B; Haier frame 230 timings; NEC power data 00 BD 01 FE LSB-first; LE16 encoding; invalid mode/range tests.
- [x] Run stdlib unittest and verify missing functionality fails.
- [x] Implement independent encoders and transport; serialize whole bursts and propagate errors.
- [x] Verify one Haier send per action, no interleaving, and pure protocol suite passing.
- [x] Commit protocol/transport and tests.

### Task 2: HA devices and configuration

**Files:** Create `__init__.py`, `const.py`, `config_flow.py`, `entity.py`, `climate.py`, `media_player.py`, `button.py`, `manifest.json`, `strings.json`, `translations/ru.json`; test `tests/ekf_appliances/test_integration.py`.

**Interfaces:** Config keys from spec; `entry.runtime_data` supplies shared transport/device identifiers. Platforms add one climate, one player, twelve buttons with stable unique IDs.

- [x] Write HA behavior tests for startup silence, failed-send rollback, off-setting changes, restore, invalid values, real sensor subscription, helper availability, power-script routing, and button command map.
- [x] Run in the isolated HA image; verify failures before implementation.
- [x] Implement selectors, validation, device identity, restoration, and platforms using installed HA APIs.
- [x] Run complete focused suite and confirm payload/state behavior.
- [x] Commit original HA implementation and tests.

### Task 3: Review and handoff

**Files:** Create integration `README.md`; update completion checkboxes and verification ledger.

- [x] Document selectors, identities, migration, assumed-state limits, sources, and exact test command.
- [x] Run complete focused suite, JSON validation, compile checks, and `git diff --check`.
- [x] Request independent whole-branch review; fix material findings RED→GREEN.
- [x] Commit and return branch/commits/tests/flow/identity/migration details without deploying.

## Verification and decisions

- Complete focused suite: 50/50 on isolated HA 2026.9.3 / Python 3.14.6, no-network, read-only code mount.
- Independent reviewers checked protocol, runtime, platforms, and flow. Concrete findings gained failing regression tests before fixes.
- First setup is off; later reload restores valid assumed mode without transmitting, per root clarification.
- Pacing includes the complete single-frame waveform duration; cancellation retains a quiet-time deadline.
- Mute stays a raw toggle button; no absolute media-player mute capability is advertised.
- Runtime API failures roll back only when surfaced; Tuya Local can swallow lower-level failures, and physical state remains assumed.
- Root supplied the scoped projector-package retirement patch; helpers/scripts/automation are preserved.
- Full unrelated repository suite was not run: host development environments are absent and its Makefile installs dependencies. No dependencies were installed.
- Worktree and branch remain intact for root integration/deployment; no approval pause, merge, push, or live restart is performed by this worker.

## Follow-up verification

The user explicitly removed the old six-frame workaround. A failing one-frame service assertion and corrected literal full-state vectors preceded the runtime/protocol changes. A registered-gateway regression exercises all fourteen canonical entity renames and exposed the deprecated via_device call; the implementation now passes the gateway ID directly.

## Health/Filter correction

- [x] Confirm issue #1894 identifies Health as a cause of the incorrect cooling indicator with heat selected; retain its literal original-remote heat capture as independent evidence.
- [x] Observe failing tests for legacy Comfort restoration, unsupported preset exposure/acceptance, and OFF-frame Health/checksum.
- [x] Remove Comfort, clear Health from all frames, and normalize legacy restoration without IR; preserve mode values and transport behavior.
- [x] Verify the focused HA suite and receive independent review approval before the scoped correction commit.

Health correction verification: five expected failures before implementation; 50/50 passing afterward in 0.924s using the isolated HA image. Compile and diff checks passed. Only climate/protocol production files changed; no device commands or live edits were made by this worker.
