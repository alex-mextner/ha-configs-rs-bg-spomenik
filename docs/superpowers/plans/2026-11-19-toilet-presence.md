# Toilet Presence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare an inactive, reusable presence-controlled light and matching ESPHome local fade module.
**Architecture:** ESPHome publishes validated presence and owns only local outputs. A first-party HA light platform combines pure policy, serialized/coalesced remote writes and an explicit manual facade.
**Tech Stack:** Python/Home Assistant, pytest/asyncio, ESPHome2026.9.1 YAML/C++, GitHub Actions.
**Spec:** docs/superpowers/specs/2026-11-19-toilet-presence-design.md

## Global Constraints

No live deployment, restart, flash, light commands, .storage edits, secret commits or unrelated light enrollment. Fade1800s, steps10s, floor min(start,10%), final native off1s when supported. Manual off>=300s AND original visit finished. Unknown never proves vacancy. Full/adversarial tests only actual CI/RunPod. Workers own disjoint paths; parent reviews; normal gates remain. Refs#92.

## Review Focus

- Repeated/already-off requests still create a fresh lock through facade and raw HA service routes.
- In-flight auto-on may complete, but newest off follows and no obsolete queued on survives.
- Unknown between absence samples resets continuous vacancy evidence.
- Restart does not complete a visit; expired deadlines release only completed original visits.
- Approach0/3/10/35% never turns off lamps on, lowers bright lamps or overrides manual suppression.

## Task1: Sensor and local fade (sensor worker)

Files: esphome/toilet-presence.yaml, esphome/libs/*, owned docs/tests.
Produces: binary_sensor.toilet_presence_occupancy; binary_sensor.toilet_presence_radar_reports_healthy; reusable local fade from current output.

- [ ] Record corridor baseline and correct LD2410 hardware.
- [ ] Add report timeout5s; no invented zones.
- [ ] Extract1800s/10s local fade; floor=min(start,10%).
- [ ] CI validate/compile with fake secrets and boundary tests.
- [ ] Commit only owned paths.

## Task2: Pure policy and writer (lighting worker)

Files: custom_components/presence_light/{__init__,policy,controller}.py; tests/presence_light/{test_policy,test_controller}.py.
Interfaces: Policy.observe_presence(occupied,now); observe_light(on,brightness); manual_off(now,wall); manual_on(now,brightness,kelvin); observe_approach(active,now); tick(now,wall,minute)->Command|None; snapshot(now,wall)->dict; restore(data,now,wall). Controller injects clock, persistence and async sender.

- [ ] Write behavioral tests before code for deadline AND visit completion, manual hold, fade and approach; run one ordinary bounded case to observe missing implementation.
- [ ] Implement deterministic reducer, generation invalidation, profiles, outages and restoration.
- [ ] Write writer cases for coalescing, in-flight off, stale final-off, storage failure and instance isolation.
- [ ] Implement one serialized writer, persisted policy changes, current-generation guard, no growing queue.
- [ ] Run bounded ordinary development case; full race/adversarial suite only CI. Commit accurate evidence.

## Task3: HA facade and inactive example (lighting worker)

Files: custom_components/presence_light/{light.py,manifest.json}; examples/toilet_presence_light.yaml; tests/presence_light/test_ha_adapter.py.
Consumes: Policy and Controller. Produces: disabled-by-default YAML light platform, explicit actuator/sensor binding, facade manual commands and raw service/state adapters.

- [ ] Write adapter cases before implementation: disabled config, facade repeated/already-off, raw HA off/toggle without user_id, own-context exclusion, unavailable light and lifecycle.
- [ ] Implement schema, unique actuator ownership, facade, Store persistence and callback cleanup. No config flow.
- [ ] Write disabled example outside loaded packages, deliberate unbound actuator placeholder, sensor IDs and activation evidence.
- [ ] Run syntax/lint checks locally; real-HA adapter and full pure suites in CI.
- [ ] Commit only lighting-owned code, tests, example and docs.

## Task4: Secrets and CI (security/provisioning worker)

Files: scripts/prepare_esphome_secrets.py, worker-owned guard/tests/CI/.gitignore.
Interfaces: local secret input -> key-name inventory OR explicit new private0700 directory with0600 prepared secrets/config. Source values remain local.

- [ ] Test inventory/no overwrite/include restrictions/redaction/permissions and guard before code.
- [ ] Implement dry-run default, explicit prepare, optional harden-source and automatic redacted/tmp logs.
- [ ] CI runs focused security/controller tests and ESPHome2026.9.1 validation/compile with fake secrets; no firmware artifact uploads.
- [ ] Commit only owned paths; provide genuine CI receipts.

## Execution record

- 2026-11-19: Read global/repository AGENTS fully. Verified RDC ubuntu-pc and owned write/read/delete. Worktree feat/toilet-presence. No live change.
- Ruling: shared behavior/parity tests instead of generated cross-runtime parameters; independently configurable instances stay simple.
- Ruling: no optional approval round under explicit autonomous instruction; independent review/CI remain.
- Ruling: user CI-only adversarial/heavy testing overrides generic skill full-suite commands. Local proof limited to ordinary one-case red/green plus syntax.
