// SPDX-License-Identifier: MIT
// Browser interaction checks. The renderer supplies the CI browser; this file never launches one.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const HOST = 'plant-care-card';
const TILE = HOST + ' button.plant-tile';
const DIALOG = HOST + ' dialog.plant-dialog';
const GRAPH = DIALOG + ' .chart';

async function recordedRun(scope, action) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'plant-card-interactions-'));
  fs.chmodSync(directory, 0o700);
  const logPath = path.join(directory, 'execution.log');
  fs.writeFileSync(logPath, '', {mode: 0o600});
  const report = {scope, synthetic: scope === 'synthetic-two-plant', checks: [], logPath};
  const log = message => {
    const line = new Date().toISOString() + ' ' + message + '\n';
    fs.appendFileSync(logPath, line);
    process.stdout.write(line);
  };
  const check = async (name, fn) => {
    await fn();
    report.checks.push(name);
    log('PASS ' + name);
  };
  log('Log: ' + logPath + ' scope=' + scope);
  try {
    await action(report, check);
    log('Completed rc=0 Log: ' + logPath);
    return report;
  } catch (error) {
    log('FAILED rc=1 ' + String(error.message) + ' Log: ' + logPath);
    error.interactionLogPath = logPath;
    throw error;
  }
}

function historyPoints(history) {
  assert.ok(history && Array.isArray(history.points), 'Recorder history is required for graph interactions');
  const start = Date.parse(history.start);
  const end = Date.parse(history.end);
  assert.ok(Number.isFinite(start) && Number.isFinite(end) && start < end);
  const points = history.points.flatMap(item => {
    const time = Date.parse(item.last_changed || item.last_updated);
    if (item.state == null || String(item.state).trim() === '') return [];
    const value = Number(item.state);
    return Number.isFinite(time)
      && Number.isFinite(value) && value >= 0 && value <= 100 ? [{time, value}] : [];
  }).sort((a, b) => a.time - b.time);
  assert.ok(points.length > 1, 'At least two real valid history points are required');
  return {start, end, points};
}

async function selectedPoint(page) {
  return page.locator(DIALOG + ' output.graph-inspection').evaluate(output => ({
    time: Number(output.dataset.timestamp),
    value: Number(output.dataset.value),
    datetime: output.querySelector('time')?.getAttribute('datetime'),
    dateText: output.querySelector('time')?.textContent.trim(),
    reading: output.querySelector('[data-reading-value]')?.textContent.trim(),
    live: output.getAttribute('aria-live'),
  }));
}

async function assertPoint(page, expected) {
  const actual = await selectedPoint(page);
  assert.equal(actual.time, expected.time, 'Graph must identify the real Recorder timestamp');
  assert.ok(Math.abs(actual.value - expected.value) < 1e-8, 'Graph must show the recorded value without interpolation');
  assert.equal(Date.parse(actual.datetime), expected.time);
  assert.ok(actual.dateText && actual.reading, 'Selection must expose visible date and moisture');
  const displayed = Number(actual.reading.match(/-?\d+(?:[.,]\d+)?/)?.[0].replace(',', '.'));
  assert.ok(Number.isFinite(displayed) && Math.abs(displayed - expected.value) <= 0.51);
  assert.match(actual.reading, /%/);
  assert.equal(actual.live, 'polite');
  const index = Number(await page.locator(GRAPH).getAttribute('aria-valuenow'));
  assert.ok(Number.isInteger(index) && index >= 0, 'Graph selection must have an accessible position');
  return actual;
}

async function openWith(page, tile, key) {
  await tile.focus();
  await tile.press(key);
  await page.locator(DIALOG).waitFor({state: 'visible'});
  assert.equal(await page.locator(DIALOG).evaluate(dialog => dialog.open), true);
}

async function assertFocusOn(page, selector) {
  assert.equal(await page.locator(selector).evaluate(element =>
    element.getRootNode().activeElement === element), true, 'Keyboard focus moved unexpectedly');
}

async function closeWithEscape(page, tileSelector) {
  await page.keyboard.press('Escape');
  await page.locator(DIALOG).waitFor({state: 'hidden'});
  await assertFocusOn(page, tileSelector);
}

async function verifyGraph(page, history, report) {
  const {start, end, points} = historyPoints(history);
  const graph = page.locator(GRAPH);
  assert.equal(await graph.getAttribute('role'), 'slider');
  assert.equal(await graph.getAttribute('tabindex'), '0');
  await graph.focus();
  await graph.press('Home');
  await assertPoint(page, points[0]);
  await graph.press('ArrowRight');
  await assertPoint(page, points[1]);
  await graph.press('End');
  await assertPoint(page, points.at(-1));
  await graph.press('ArrowLeft');
  await assertPoint(page, points.at(-2));

  await graph.scrollIntoViewIfNeeded();
  await graph.evaluate(element => {
    element.addEventListener('pointerdown', event => {
      const bounds = element.getBoundingClientRect();
      window.__plantQAPointer = {x: event.clientX, left: bounds.left, width: bounds.width};
    }, {once: true, capture: true});
  });
  const bounds = await graph.boundingBox();
  assert.ok(bounds && bounds.width > 0 && bounds.height > 0);
  const touch = await page.evaluate(() => navigator.maxTouchPoints > 0);
  const x = bounds.x + bounds.width * 0.37;
  const y = bounds.y + bounds.height * 0.5;
  if (touch) await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
  const pointer = await page.evaluate(() => {
    const result = window.__plantQAPointer;
    delete window.__plantQAPointer;
    return result;
  });
  assert.ok(pointer && pointer.width > 0, 'Real pointer input did not reach the graph');
  const requestedTime = start + (pointer.x - pointer.left) / pointer.width * (end - start);
  const distance = Math.min(...points.map(point => Math.abs(point.time - requestedTime)));
  const nearest = points.filter(point => Math.abs(Math.abs(point.time - requestedTime) - distance) < 0.01);
  const selected = await selectedPoint(page);
  const expected = nearest.find(point => point.time === selected.time && point.value === selected.value);
  assert.ok(expected, 'Pointer must select the nearest real Recorder sample');
  await assertPoint(page, expected);
  report.pointerInput = touch ? 'native-touch' : 'mouse';
  report.recordedPoints = points.length;
}

async function prepareStabilityProbe(page) {
  const sections = [];
  for (const selector of ['.calibration-details', '.watering-details']) {
    const locator = page.locator(DIALOG + ' ' + selector);
    if (await locator.count() === 0) continue;
    const wasOpen = await locator.evaluate(details => details.open);
    sections.push({selector, wasOpen});
    if (!wasOpen) await locator.locator('summary').click();
  }
  await page.locator(GRAPH).focus();
  await page.evaluate(() => {
    const card = document.querySelector('plant-care-card');
    const root = card.shadowRoot;
    const nodes = [...root.querySelectorAll(
      '.plant-tile, .plant-tile img, dialog.plant-dialog, dialog .plant, dialog .photo, dialog .chart, .graph-inspection, .calibration-details, .watering-details'
    )];
    const details = [...root.querySelectorAll('.calibration-details, .watering-details')].map(node => [node, node.open]);
    const output = root.querySelector('.graph-inspection');
    const probe = {
      nodes, details, removed: [],
      graph: root.querySelector('dialog .chart'),
      dialog: root.querySelector('dialog.plant-dialog'),
      range: root.querySelector('dialog [data-action="range"][aria-pressed="true"]')?.dataset.days,
      selected: [output?.dataset.timestamp, output?.dataset.value],
    };
    probe.observer = new MutationObserver(records => {
      for (const record of records) for (const removed of record.removedNodes) {
        for (const node of nodes) if (removed === node || removed.contains(node)) {
          probe.removed.push(node.tagName + '.' + node.classList.value);
        }
      }
    });
    probe.observer.observe(root, {childList: true, subtree: true});
    window.__plantQAProbe = probe;
  });
  return {sections, hasDisclosure: sections.some(item => item.selector === '.calibration-details')};
}

async function assertStableAfterUpdate(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const result = await page.evaluate(() => {
    const card = document.querySelector('plant-care-card');
    const root = card.shadowRoot;
    const probe = window.__plantQAProbe;
    const output = root.querySelector('.graph-inspection');
    return {
      removed: probe.removed,
      connected: probe.nodes.every(node => node.isConnected),
      sameGraph: root.querySelector('dialog .chart') === probe.graph,
      sameDialog: root.querySelector('dialog.plant-dialog') === probe.dialog,
      dialogOpen: probe.dialog.open,
      focused: root.activeElement === probe.graph,
      range: root.querySelector('dialog [data-action="range"][aria-pressed="true"]')?.dataset.days,
      expectedRange: probe.range,
      selected: [output?.dataset.timestamp, output?.dataset.value],
      expectedSelected: probe.selected,
      disclosuresPreserved: probe.details.every(([node, open]) => node.open === open),
    };
  });
  assert.deepEqual(result.removed, [], 'A live update removed a tile, photo, graph or dialog node');
  for (const key of ['connected', 'sameGraph', 'sameDialog', 'dialogOpen', 'focused', 'disclosuresPreserved']) {
    assert.equal(result[key], true, 'Live update failed to preserve ' + key);
  }
  assert.equal(result.range, result.expectedRange);
  assert.deepEqual(result.selected, result.expectedSelected);
}

async function stopStabilityProbe(page, disclosure) {
  await page.evaluate(() => {
    window.__plantQAProbe?.observer.disconnect();
    delete window.__plantQAProbe;
  });
  for (const section of disclosure.sections) {
    const details = page.locator(DIALOG + ' ' + section.selector);
    if (await details.evaluate(element => element.open) !== section.wasOpen) {
      await details.locator('summary').click();
    }
  }
}

function displayedNumber(text) {
  const match = text.match(/-?\d+(?:[.,]\d+)?/);
  return match ? Number(match[0].replace(',', '.')) : NaN;
}

async function verifyCardInteractions(page, snapshot) {
  return recordedRun('observed-single-plant', async (report, check) => {
    assert.equal(snapshot.schema, 'plant-care-live-snapshot/v1');
    assert.equal(snapshot.source, 'Home Assistant REST API');
    const tile = page.locator(TILE);
    const attributes = snapshot.plant.attributes;
    await check('one compact informative tile for the observed plant', async () => {
      assert.equal(await tile.count(), 1);
      assert.equal(await tile.getAttribute('data-entity'), snapshot.plant.entity_id);
      assert.equal(await tile.locator('.tile-name').innerText(), attributes.plant_name);
      for (const field of ['.tile-moisture', '.tile-status', '.tile-watering']) {
        assert.ok((await tile.locator(field).innerText()).trim(), 'Missing compact field ' + field);
      }
      assert.ok((await tile.innerText()).length <= 260, 'Dashboard tile contains excessive detail');
      assert.equal(await tile.locator('.facts, .graph, .calibration-details, .actions').count(), 0);
      assert.equal(await page.locator(DIALOG).evaluate(dialog => dialog.open), false);
      if (snapshot.plant.state === 'sensor_error') {
        assert.match(await tile.locator('.tile-moisture').innerText(), /—/);
        assert.match(await tile.locator('.tile-status').innerText(), /Недостоверные/);
      } else if (snapshot.plant.state === 'calibrating') {
        assert.match(await tile.locator('.tile-moisture').innerText(), /—/);
      } else if (attributes.reliability_valid && Number.isFinite(attributes.moisture)) {
        const shown = displayedNumber(await tile.locator('.tile-moisture').innerText());
        assert.ok(Math.abs(shown - attributes.moisture) <= 0.51, 'Tile does not show the observed reading');
      }
    });
    await check('Enter, Space and pointer open details; Escape and close return focus', async () => {
      await openWith(page, tile, 'Enter');
      assert.equal(await page.locator(DIALOG + ' .detail-content .plant').getAttribute('data-entity'), snapshot.plant.entity_id);
      await closeWithEscape(page, TILE);
      await openWith(page, tile, 'Space');
      await page.locator(DIALOG + ' [data-action="close-details"]').click();
      await page.locator(DIALOG).waitFor({state: 'hidden'});
      await assertFocusOn(page, TILE);
      await tile.click();
      await page.locator(DIALOG).waitFor({state: 'visible'});
    });
    if (attributes.picture) await check('observed photo loads in tile and detail', async () => {
      const images = page.locator(TILE + ' img, ' + DIALOG + ' img');
      assert.equal(await images.count(), 2);
      await page.waitForFunction(() => {
        const root = document.querySelector('plant-care-card').shadowRoot;
        return [...root.querySelectorAll('.plant-tile img, dialog[open] img')]
          .every(image => image.complete && image.naturalWidth > 0);
      });
      const actualPaths = await images.evaluateAll(elements => elements.map(image => new URL(image.currentSrc).pathname));
      assert.deepEqual(actualPaths, [0, 1].map(() => new URL(attributes.picture, 'https://ha.invalid').pathname));
    });
    await check('keyboard and real pointer select recorded date and value', () => verifyGraph(page, snapshot.history, report));
    const disclosure = await prepareStabilityProbe(page);
    await page.evaluate(() => { window.__plantQAOriginalHass = document.querySelector('plant-care-card').hass; });
    try {
      await check('ordinary HA updates preserve DOM, focus, range, point and disclosure', async () => {
        for (const kind of ['same-object', 'unrelated-state', 'equivalent-plant-copy']) {
          await page.evaluate(({kind, entity}) => {
            const card = document.querySelector('plant-care-card');
            const current = card.hass;
            if (kind === 'same-object') card.hass = current;
            else if (kind === 'unrelated-state') card.hass = {...current, states: {
              ...current.states, 'sensor.qa_unrelated': {entity_id: 'sensor.qa_unrelated', state: '42', attributes: {}}
            }};
            else card.hass = {...current, states: {
              ...current.states, [entity]: structuredClone(current.states[entity])
            }};
          }, {kind, entity: snapshot.plant.entity_id});
          await assertStableAfterUpdate(page);
        }
      });
    } finally {
      await stopStabilityProbe(page, disclosure);
      await page.evaluate(() => {
        document.querySelector('plant-care-card').hass = window.__plantQAOriginalHass;
        delete window.__plantQAOriginalHass;
      });
    }
    await closeWithEscape(page, TILE);
    report.finalView = 'dashboard';
    report.factsMutated = false;
    report.calibrationDisclosureObserved = disclosure.hasDisclosure;
    report.wateringDisclosureObserved = disclosure.sections.some(item => item.selector === '.watering-details');
  });
}

function syntheticFixture() {
  const now = Date.parse('2026-11-18T12:00:00Z');
  const definitions = [
    {id: 'sensor.qa_plant_a', name: 'Тестовое растение A', source: 'sensor.qa_soil_a', moisture: 55, values: [70, 64, 52, 45]},
    {id: 'sensor.qa_plant_b', name: 'Тестовое растение B', source: 'sensor.qa_soil_b', moisture: 71, values: [92, 80, 65, 61]},
  ];
  const states = {};
  const histories = {};
  for (const [index, item] of definitions.entries()) {
    states[item.id] = {entity_id: item.id, state: 'ok', attributes: {
      plant_care: true, plant_name: item.name, source_entity: item.source, entry_id: 'qa-entry-' + index,
      moisture: item.moisture, raw_moisture: item.moisture, reliability_valid: true,
      last_sample_at: now / 1000, last_value_change_at: now / 1000, dry_threshold: 25,
      last_watering_at: '2026-11-17T08:30:00Z', last_watering_source: 'auto',
      last_watering_precision: 'minute', forecast_status: 'insufficient_data',
      watering_history: [{event_id: 'qa-watering-' + index, at: '2026-11-17T08:30:00Z',
        detected_at: '2026-11-17T08:45:00Z', source: 'auto', precision: 'minute',
        retroactive: false, valid: true}],
      calibration_enabled: true, calibration_status: 'awaiting_reference', calibration_revision: 1,
      calibration_dry_adc: 67, calibration_wet_adc: 1298, calibration_baseline_dry: 67,
      calibration_baseline_wet: 1298, calibration_history_available: true,
      calibration_history: [{event_id: 'qa-event-' + index, at: (now - 86400000) / 1000,
        detected_at: (now - 86400000) / 1000, kind: 'manual', reason: 'manual',
        old_dry: 67, old_wet: 221, new_dry: 67, new_wet: 1298, revision: 1}],
    }};
    for (const days of [1, 7]) {
      const start = now - days * 86400000;
      const points = item.values.map((value, point) => ({
        state: String(value - (days === 1 ? 2 : 0)),
        last_changed: new Date(start + (point + 1) / 5 * (now - start)).toISOString(),
      }));
      points.splice(2, 0, {state: 'unavailable', last_changed: new Date(start + 0.5 * (now - start)).toISOString()});
      histories[item.source + '|' + days] = {entity_id: item.source,
        start: new Date(start).toISOString(), end: new Date(now).toISOString(), points};
    }
  }
  return {now, definitions, states, histories};
}

async function verifySyntheticCardInteractions(page) {
  return recordedRun('synthetic-two-plant', async (report, check) => {
    assert.equal(await page.locator(HOST).count(), 0, 'Synthetic checks require a separate empty page');
    const fixture = syntheticFixture();
    await page.clock.setFixedTime(new Date(fixture.now));
    await page.evaluate(fixture => {
      const card = document.createElement('plant-care-card');
      card.dataset.qaScenario = 'synthetic-two-plant';
      document.body.appendChild(card);
      card.setConfig({entities: fixture.definitions.map(item => item.id)});
      for (const [key, history] of Object.entries(fixture.histories)) card._history[key] = {
        points: history.points, start: Date.parse(history.start), end: Date.parse(history.end),
        loading: false, fetchedAt: fixture.now,
      };
      card.hass = {states: {...fixture.states,
        'input_boolean.plant_care_voice_enabled': {state: 'off'}}};
    }, fixture);
    const first = TILE + '[data-entity="sensor.qa_plant_a"]';
    const second = TILE + '[data-entity="sensor.qa_plant_b"]';
    try {
      await check('two plants produce two independently labelled tiles', async () => {
        assert.equal(await page.locator(TILE).count(), 2);
        assert.equal(await page.locator(first + ' .tile-name').innerText(), fixture.definitions[0].name);
        assert.equal(await page.locator(second + ' .tile-name').innerText(), fixture.definitions[1].name);
        await page.locator(first).click();
        assert.equal(await page.locator(DIALOG + ' .plant').getAttribute('data-entity'), 'sensor.qa_plant_a');
      });
      await check('range controls select the requested history', async () => {
        await page.locator(DIALOG + ' [data-action="range"][data-days="1"]').click();
        assert.equal(await page.locator(DIALOG + ' [data-action="range"][aria-pressed="true"]').getAttribute('data-days'), '1');
        await verifyGraph(page, fixture.histories['sensor.qa_soil_a|1'], report);
      });
      const disclosure = await prepareStabilityProbe(page);
      try {
        await check('changed readings update the correct tiles without recreating the active detail', async () => {
          for (const [entity, moisture] of [['sensor.qa_plant_a', 54.4], ['sensor.qa_plant_b', 73.2]]) {
            await page.evaluate(({entity, moisture}) => {
              const card = document.querySelector('plant-care-card');
              const state = card.hass.states[entity];
              card.hass = {...card.hass, states: {...card.hass.states, [entity]: {
                ...state, attributes: {...state.attributes, moisture, raw_moisture: moisture, last_sample_at: state.attributes.last_sample_at + 10},
              }}};
            }, {entity, moisture});
            await assertStableAfterUpdate(page);
            const shown = displayedNumber(await page.locator(TILE + '[data-entity="' + entity + '"] .tile-moisture').innerText());
            assert.ok(Math.abs(shown - moisture) <= 0.51, 'Updated reading is missing from its plant tile');
          }
          assert.equal(await page.locator(DIALOG + ' .plant').getAttribute('data-entity'), 'sensor.qa_plant_a');
          assert.ok(await page.locator(DIALOG + ' .calibration-history').isVisible(), 'Calibration history must remain visible');
          assert.equal(await page.locator(DIALOG + ' .calibration-event').count(), 1);
          assert.ok(await page.locator(DIALOG + ' .watering-history').isVisible(), 'Watering history must remain visible');
          assert.equal(await page.locator(DIALOG + ' .watering-event').count(), 1);
        });
      } finally {
        await stopStabilityProbe(page, disclosure);
      }
      const firstSelection = await selectedPoint(page);
      await check('switching plants uses each real history and restores the first selection', async () => {
        await closeWithEscape(page, first);
        await page.locator(second).click();
        assert.equal(await page.locator(DIALOG + ' .plant').getAttribute('data-entity'), 'sensor.qa_plant_b');
        const days = await page.locator(DIALOG + ' [data-action="range"][aria-pressed="true"]').getAttribute('data-days');
        const secondHistory = historyPoints(fixture.histories['sensor.qa_soil_b|' + days]);
        await page.locator(GRAPH).focus();
        await page.locator(GRAPH).press('Home');
        await assertPoint(page, secondHistory.points[0]);
        await closeWithEscape(page, second);
        await page.locator(first).click();
        await page.locator(DIALOG + ' [data-action="range"][data-days="1"]').click();
        await assertPoint(page, firstSelection);
        await closeWithEscape(page, first);
      });
      report.actualScreenshotsAllowed = false;
    } finally {
      await page.evaluate(() => {
        window.__plantQAProbe?.observer.disconnect();
        delete window.__plantQAProbe;
        document.querySelector('plant-care-card[data-qa-scenario="synthetic-two-plant"]')?.remove();
      });
    }
  });
}

module.exports = {verifyCardInteractions, verifySyntheticCardInteractions};
