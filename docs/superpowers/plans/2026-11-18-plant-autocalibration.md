# Plant Autocalibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поддерживать небольшие подтверждённые изменения мокрой опоры датчика без LLM, частых перенастроек и вмешательства пользователя; каждое фактическое изменение видно в сохраняемой истории.

**Architecture:** Чистый детерминированный engine оценивает сырые физические измерения и независимо подтверждённые опорные циклы. HA runtime владеет конфигурацией, долговременным Store, сериализацией, проверкой ожидаемой ревизии, записью helper и восстановлением незавершённой операции. Карточка отображает текущее решение и журнал, сохраняя существующую ошибку достоверности.

**Tech Stack:** Python, Home Assistant Store/services, существующий JavaScript custom card, native GitHub Actions и Playwright для компонентных изображений.

**Spec:** [Автокалибровка датчика влажности](../../plant-autocalibration.md).

## Global Constraints

- Runtime не вызывает LLM, OMP, удалённую модель или уведомления для решения о калибровке.
- Исходные сухая/мокрая точки и размах неизменны; меняется только мокрая точка.
- Три новых независимых цикла охватывают ≥14 дней; отдельный будущий цикл служит проверкой.
- Стационарное окно 1–2 часа: ≥12 последовательно выбранных физических наблюдений с интервалом ≥270 секунд (10% допуска к номинальным 300 секундам), без UTC-buckets. Покрытие ≥3600 секунд, максимальный разрыв ≤600 секунд и края ≤300 секунд проверяются по всем уникальным качественным отсчётам.
- Полная оценка должна лежать в ±5% исходного размаха; применённый шаг ≤1%, между обычными коррекциями ≥7 суток. Целевая поправка должна превышать мёртвую зону `max(2, 0.005 × S0)` до ограничения применяемого шага.
- Все сравнительные ошибки считаются до ограничения индекса до 0–100; подтверждённый защитный откат может выполняться раньше семи суток.
- 67/221 и текущий сырой сигнал около 1300–1500 не дают разрешения изменить опору.
- Физические часы, открытый инцидент и записи полива не изменяются из-за пересчёта шкалы.
- Полные тесты и браузерный render выполняются только в native CI. Локально допустимы ограниченные проверки изменённых файлов и отдельных тестов.
- Каждый владелец редактирует только свои файлы; staging и commit согласуются для целостного кандидата.

## Review Focus

- Повтор одного пакета, одинаковые timestamps и плотный burst не должны выдавать независимое часовое наблюдение.
- Полная оценка за пределами исходного диапазона не должна проникать через ограничение маленького шага. Статистика учитывает все уникальные качественные отсчёты без отбора по будущему целевому диапазону; временное прореживание проверяет только независимое количество. Значения на физических границах ADC не подтверждают опору.
- Перезапуск между намерением и подтверждением записи не должен менять helper дважды или терять запись фактического изменения.
- Ручная правка и смена источника не должны сбрасывать исходный диапазон, паузу или повторно использовать чужие циклы.
- Изменение коэффициента не должно закрывать прежний инцидент, создавать полив или смешивать несовместимые ревизии истории.

---

### Task 1: Deterministic calibration engine

**Files:** `custom_components/plant_care_alerts/calibration.py`; `tests/plant_care/test_calibration.py`.

**Interfaces:** `evaluate_calibration(samples: Sequence[PhysicalSample], references: Sequence[QualifiedWetReference], state: CalibrationState, now: float) -> CalibrationDecision`. Frozen dataclasses carry source identity, explicit reference index, immutable baseline and persisted revision/cooldown/evidence IDs. Decisions use `observe`, `propose` or `rollback`; only the latter two may supply a writable endpoint.

- [ ] Add cases for an isolated impulse, clustered outliers, zero MAD, a replayed burst, a 55-minute window and missing coverage. Outliers alone cannot establish a reference; an otherwise qualified window with one impulse remains stable. Replayed or insufficiently covered evidence cannot qualify a cycle.
- [ ] Add cases for three distinct training cycles spanning 14 days plus a later unused holdout. No reference, repeated evidence, stale/future data and wrong source identity must produce observation only.
- [ ] Implement median/MAD window summaries, fixed-baseline target calculation, the full-target envelope, quantized step, deadband and persistent seven-day cooldown.
- [ ] Prove 1500 with baseline67/221 cannot produce a writable endpoint, even if a small step would fit. Verify upward and downward wet-point drift within the supported `D0 < W0` physical scale and a reference index of90; a separately qualified100 is also valid.
- [ ] Compare unclipped holdout errors; test qualified contradiction and a rollback recommendation against the exact prior revision. A rejected proposal never mutates input state.
- [ ] Run the bounded engine file and strict pinned Ruff; retain real failure/pass output. Freeze source bytes for the integrated review.

### Task 2: HA persistence and safe application

**Files:** `custom_components/plant_care_alerts/calibration_runtime.py`, `calibration_storage.py`, `__init__.py`, `reliability.py`, `sensor.py`; `packages/plant_care.yaml`; `tests/plant_care/test_calibration_runtime.py`, `test_calibration_storage.py`, `test_calibration_native_service.py`, `test_adapter.py` and HA adapter cases as required.

**Interfaces:** `CalibrationRuntime(hass, store, config)` exposes `async_load()`, `async_prepare(entry_id, source_id, now, registry_identity='')`, `seed_history(entry_id, states, quality, now)`, `async_observe(entry_id, quality, now)`, `reading_fields(entry_id)`, `attributes(entry_id)`, `async_record_reference(entry_id, now)` and `async_save()`. Store `plant_care_calibration`, version1, is independent of the existing care Store. Service `plant_care_alerts.record_reference` accepts the care entity and reads only its configured independent reference entity and protocol.

- [ ] Test reference ingestion against the configured allowlist, source identity and explicit protocol/index; caller-supplied arbitrary evidence and a plain user watering report cannot qualify.
- [ ] Test unchanged defaults in the no-reference deployment, bounded raw-history persistence beyond the analytics/Recorder window and deduplicated qualified cycles.
- [ ] Persist immutable baseline, revision, pending operation and actual-change journal. Test save/write failure, restart reconciliation and competing manual changes without duplicate application or lost history.
- [ ] Register an own-domain admin action through the documented `async_register_admin_service`, with explicit helper `POLICY_CONTROL`, preserved caller context and a fresh in-handler compare after all authorization awaits. Do not call the internal entity-service registration helper or replace native `set_value`. Prove pending-save, queued-after and synchronous call-event competition with real HA services; fail closed outside the tested native runtime contract.
- [ ] Distinguish first initialization from a missing or malformed record in an existing Store; explicit original bounds, cooldown and used evidence must never be silently reset.
- [ ] Test two rapid manual changes221→222→223: two journal entries before refresh coalescing. Test external/manual changes: journal old/new values, revision increment, 30-minute barrier and seven-day cooldown; no rebase of the original span. A new source stays quarantined until separate commissioning.
- [ ] Test frozen-incident continuity and physical clocks across calibration. The previous basis and normal three-sample/ten-minute recovery remain authoritative; calibration does not infer watering.
- [ ] Run bounded runtime tests and exact changed-file checks; full installed-HA adapter coverage runs in native CI.

### Task 3: Visible history and release evidence

**Files:** `www/plant-care/plant-care-card.js`; `tests/plant_care/card_autocalibration.test.cjs`; `docs/plant-autocalibration.md`, `docs/plant-care/README.md`; `.github/workflows/plant-care.yml`; `tests/plant_care/render_component.cjs`, `tests/plant_care/fixtures/autocalibration-before.json`, `tests/plant_care/fixtures/live-reading.json`.

**Interfaces:** Care attributes expose current calibration status/reason and actual-change history from `CalibrationRuntime.attributes()`. The renderer accepts a sanitized real API snapshot, freezes the clock at `observed_at` and records card/source/snapshot hashes. Its optional `--card-revision` uses an immutable historical commit for the before image.

- [ ] Add card assertions for observation without a reference, an actual auto/manual change, a rollback and an empty journal. Keep unreliable moisture hidden and escape source/reason text.
- [ ] Show concise status and actual dated changes; routine polls and rejected candidates must not appear as recalibrations.
- [ ] Run ordinary complete pre-commit hooks on the coordinated staged candidate and the current task's required independent native reviews. Read actual verdicts and repair mandatory findings.
- [ ] Push an ordinary scoped PR linked to the canonical repository issue. Run all native CI checks, including installed-HA tests and before/current component rendering; no old task's review or CI counts as this feature's proof.
- [ ] Deploy only reviewed, verified bytes through backup/CAS/config checks. Read live state, confirm current67/221 remain unchanged and history/status are truthful; capture a new sanitized after snapshot.
- [ ] Re-render that exact after snapshot in native CI, verify the actual source hash and before/after images, complete native acceptance and normal `gh ship`, then verify published main and deployed bytes.
- [ ] Archive required evidence and remove only owned scratch/worktrees through the normal protected cleanup path.
