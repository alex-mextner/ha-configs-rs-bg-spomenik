# Plant Care Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans.

**Goal:** deploy tested, persistent, multi-plant watering alerts and a dashboard card.
**Architecture:** pinned upstream Adaptive Plant + companion state sensor + native HA delivery automation.
**Tech Stack:** Python/HA, unittest, YAML/Jinja, plain web component.
**Spec:** ../specs/2026-10-27-plant-care-design.md

## Global Constraints
MIT for new reusable code; no .storage edits; no unrelated commits; preserve ADC 67/221; no sidebar; voice 08–22; no watering actions.

## Review Focus
- stale, missing or non-finite sensor data cannot become a dry warning;
- restarts/gaps cannot manufacture six hours of continuous dryness;
- repeated unchanged reads are not independent samples;
- manual watering/snooze must stop voice;
- removal/new plants, renamed entities and photo URLs must remain safe.

### Task 1: alert engine and HA adapter
Files: custom_components/plant_care_alerts/{state_machine,__init__,sensor}.py, manifest.json, LICENSE; tests/plant_care/test_state_machine.py.
- [ ] Write failing tests for robust mean, confirmation/overdue, hysteresis, stale data, restart, acknowledgments and schedules.
- [ ] Implement evaluate(previous, reading, now, settings) -> serializable dict; run unittest.
- [ ] Adapter discovers upstream entries/registry IDs, persists through HA Store, emits transition events and sensor attributes. Commit.

### Task 2: card and delivery
Files: www/plant-care/plant-care-card.js; packages/plant_care.yaml; blueprints/automation/plant_care/alerts.yaml; tests/plant_care/test_config.py.
- [ ] Add tests for quiet hours, hourly cadence, verified notification/speaker targets, generic discovery, no sidebar.
- [ ] Implement dynamic card, native buttons/options links and blueprint. Validate YAML, JS and HA check_config.

### Task 3: deployment and evidence
- [ ] Add upstream submodule/symlink. Deploy only new scoped files and additive resource lines, with backups.
- [ ] Create Strelitzia via HA config flow, set real existing source; record yesterday's reported watering only.
- [ ] Add card to existing Home dashboard via API; verify live sensors/settings/photo upload form.
- [ ] Send labelled notification test, test quiet-hour suppression, capture real browser live/preview screenshots.
- [ ] Run full available suite, review diff, commit scoped changes, report any blocked checks honestly.

## Execution ledger
- 2026-10-27: isolated clone from production HEAD; unrelated staged/dirty production work preserved.
- Engine/config/card tests observed failing before implementation, then green.
- Review findings reproduced and fixed: unavailable raw ADC, temporary upstream unload, lost initial event, source replacement, duplicate recovery push, snooze semantics, stale-window mismatch, missing source policy.
- Full local `python3 -m pytest -q`: collection blocked by 60 errors from missing original HA/HGA/desktop dependencies and uninitialized unrelated submodules; not counted as passing. See /tmp/plant-care-full-suite.log.
- Production scoped files copied with backups, independent HA check_config exited 0; safe reload in progress.

## Verified deployment evidence
- Adaptive Plant entry created through HA config flow: Strelitzia; real last watering 2026-10-26; source is median soil index, dry=25, upstream auto-water threshold=100. ADC calibration remains 67/221.
- Existing native Home overview converted by HA's built-in dashboard migration to editable Overview; all 10 original views/room subviews preserved. No plant sidebar panel added. Backup: /home/ultra/backups/plant-care-browser-20261027/home-before.json.
- Browser screenshots: actual desktop/mobile normal card, clearly labelled read-only orange/red previews, and native photo upload/options form. Saved outside Git under ~/.cache/plant-care-20261027.
- Live blueprint tested with a temporary isolated test entity: orange persistent warning, red update, normal dismissal. Test entity removed in finally; real plant data untouched. Labelled phone push service completed.
- HA condition.time boundary tests cover 07:59/08:00/21:59/22:00/01:00; no forced audio during quiet hours.
- Additional review regression cases: durable delivery acknowledgements, notification cleanup on removal/failure, no retry loop after acknowledged red downgrades to orange, short ADC glitches pause rather than erase dry confirmation.
