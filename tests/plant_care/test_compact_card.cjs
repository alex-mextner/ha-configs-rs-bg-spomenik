// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
let Card;
const context = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {innerHTML: '', querySelectorAll: () => []}; } },
  customElements: {get: () => null, define: (_name, cls) => { Card = cls; }},
  window: {}, location: {origin: 'http://ha.invalid'}, URL, Date, Intl, Set, Map, Number,
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../www/plant-care/plant-care-card.js'), 'utf8'), context);
const card = new Card();
card._hass = {states: {}};
const plant = {
  entity_id: 'sensor.strelitzia',
  state: 'ok',
  attributes: {
    plant_care: true, entry_id: 'plant-entry', plant_name: 'Стрелиция',
    moisture: 97.6, reliability_valid: true,
    source_entity: 'sensor.moisture',
    picture: '/local/plant_care/managed/example.png',
    last_watering_at: '2026-11-17',
    last_watering_source: 'user_report',
    last_watering_precision: 'approximate_day',
  },
};
assert.equal(typeof card._tile, 'function', 'The requested compact tile is missing');
const tile = card._tile(plant);
assert.match(tile, /class="plant-tile/);
assert.match(tile, /data-action="open"/);
assert.match(tile, /data-entity="sensor.strelitzia"/);
assert.match(tile, /Стрелиция/);
assert.match(tile, /98/);
assert.match(tile, /≈.*17/);
assert.doesNotMatch(tile, /00:00|12:00|class="facts"|class="graph"/);
assert.equal((tile.match(/<button\b/g) || []).length, 1, 'The photograph belongs to the same tile button');
assert.doesNotMatch(tile, /data-action="photo"/);

const bad = {...plant, state: 'sensor_error', attributes: {...plant.attributes, moisture: 100, reliability_valid: false}};
const invalid = card._tile(bad);
assert.match(invalid, /Недостоверные показания/);
assert.match(invalid, /—/);
assert.doesNotMatch(invalid, /100\s*(?:<|%)/);
const unsafe = card._tile({...plant, attributes: {...plant.attributes, plant_name: '<img src=x onerror=alert(1)>'}});
assert.doesNotMatch(unsafe, /<img src=x/);
assert.match(unsafe, /&lt;img/);
assert.match(card._wateringFact(plant.attributes), /примерно.*17/);
assert.doesNotMatch(card._wateringFact(plant.attributes), /00:00|12:00/);

const start = Date.parse('2026-11-18T12:00:00Z');
const stamp = minutes => new Date(start + minutes * 60000).toISOString();
card._history['sensor.moisture|7'] = {
  start, end: start + 3600000, loading: false, fetchedAt: start, includesInitialState: false,
  points: [
    {state: '20', last_changed: stamp(0)},
    {state: '25', last_changed: stamp(15)},
    {state: 'unknown', last_changed: stamp(30)},
    {state: '70', last_changed: stamp(45)},
    {state: '60', last_changed: stamp(60)},
  ],
};
assert.equal(typeof card._nearestHistoryPoint, 'function');
const inspected = card._nearestHistoryPoint(plant.attributes, 0.48);
assert.equal(inspected.time, start + 15 * 60000);
assert.equal(inspected.value, 25, 'Unknown points and interpolated values are not observations');
assert.equal(card._nearestHistoryPoint(plant.attributes, -1).value, 20);
assert.equal(card._nearestHistoryPoint(plant.attributes, 2).value, 60);
const graph = card._graph(plant.attributes, plant.entity_id);
assert.match(graph, /tabindex="0"/);
assert.match(graph, /role="slider"/);
assert.match(graph, /class="graph-inspection"/);
assert.match(graph, /aria-live="polite"/);
test('legacy boundary is drawn but only actual observations can be selected', () => {
  const h = {start, end: start + 3600000, includesInitialState: true, points: [
    {state: '61', last_changed: stamp(0)},
    {state: '62', last_changed: stamp(0)},
    {state: '65', last_changed: stamp(15)},
  ]};
  assert.deepEqual(Array.from(card._historyPoints(h), point => point.value), [62,65]);
  assert.equal(card._historyShapes(h).count, 3, 'Carry-forward remains drawing context');
  h.includesInitialState = false;
  assert.deepEqual(Array.from(card._historyPoints(h), point => point.value), [61,62,65]);
});

test('normalization is reused and refreshed when the history response changes', () => {
  const h = {start, end: start + 3600000, includesInitialState: false,
    points: [{state: '20', last_changed: stamp(15)}]};
  const first = card._historyPoints(h);
  assert.strictEqual(card._historyPoints(h), first);
  h.points = [{state: '40', last_changed: stamp(20)}];
  const refreshed = card._historyPoints(h);
  assert.notStrictEqual(refreshed, first);
  assert.equal(refreshed[0].value, 40);
});

test('failed refresh of an empty cached response exposes error and retry', () => {
  card._history['sensor.moisture|7'] = {start, end: start + 3600000, points: [], error: true};
  const failed = card._graph(plant.attributes, plant.entity_id);
  assert.match(failed, /Не удалось загрузить историю/);
  assert.match(failed, /data-action="retry"/);
  assert.doesNotMatch(failed, /Нет записей/);
});

test('live Recorder requests omit synthetic start-of-period state', () => {
  const fresh = new Card(); let requested;
  fresh._selectedEntity = plant.entity_id;
  fresh._hass = {states: {[plant.entity_id]: plant}, callApi: (_method, endpoint) => {
    requested = endpoint; return new Promise(() => {});
  }};
  fresh._loadHistory();
  assert.match(requested, /(?:[?&])skip_initial_state(?:&|$)/);
});

test('drag and keyboard selection update only inspection and skip the same observation', () => {
  const fresh = new Card(); let writes = 0;
  const node = () => ({attributes: {}, setAttribute(name, value) {
    this.attributes[name] = String(value); writes++;
  }});
  const chart = node(), line = node(), marker = node(), output = node(), time = node(), value = node();
  chart.getBoundingClientRect = () => ({left: 0, width: 1000});
  chart.matches = () => true;
  chart.closest = () => chart;
  chart.querySelector = selector => ({'.inspection-line': line, '.inspection-point': marker})[selector];
  output.querySelector = selector => ({time, '[data-reading-value]': value})[selector];
  fresh.shadowRoot.querySelector = selector => ({'.chart': chart, '.graph-inspection': output})[selector];
  fresh._selectedEntity = plant.entity_id;
  fresh._hass = {states: {[plant.entity_id]: plant}};
  fresh._history['sensor.moisture|7'] = {start, end: start + 3600000, includesInitialState: false,
    points: [{state: '20', last_changed: stamp(15)}, {state: '70', last_changed: stamp(45)}]};
  fresh._selectedPoints.set(`${plant.entity_id}|7`, fresh._historyPoints(fresh._history['sensor.moisture|7'])[0]);
  fresh._render = () => assert.fail('Selection must not rebuild detail markup');
  fresh._inspectPointer({target: chart, clientX: 250});
  assert.equal(writes, 0, 'Selecting the existing point performs no DOM work');
  fresh._inspectPointer({target: chart, clientX: 750});
  assert.equal(chart.attributes['aria-valuenow'], '1');
  assert.equal(line.attributes.x1, '750');
  assert.equal(marker.attributes.cy, '30');
  assert.equal(output.attributes['data-value'], '70');
  assert.equal(time.attributes.datetime, stamp(45));
  assert.equal(value.textContent, '70 %');
  const afterMove = writes;
  fresh._inspectPointer({target: chart, clientX: 749});
  assert.equal(writes, afterMove, 'Movement within the same nearest observation is inert');
  fresh._inspectKeyboard({target: chart, key: 'Home', preventDefault() {}});
  assert.equal(chart.attributes['aria-valuenow'], '0');
  assert.equal(time.attributes.datetime, stamp(15));
});

test('equal-time observations remain independently selectable and survive refreshed rendering', () => {
  for (const timestamps of [[stamp(0), stamp(0)],
    ['2026-11-18T12:00:00.000100Z', '2026-11-18T12:00:00.000200Z']]) {
    const fresh = new Card(); let inspected;
    const chart = {matches: () => true};
    fresh._selectedEntity = plant.entity_id;
    fresh._hass = {states: {[plant.entity_id]: plant}};
    const h = fresh._history['sensor.moisture|7'] = {
      start, end: start + 3600000, includesInitialState: false,
      points: [{state: '61', last_changed: timestamps[0]},
        {state: '62', last_changed: timestamps[1]}, {state: '65', last_changed: stamp(15)}],
    };
    fresh._updateInspection = (_history, _points, point) => { inspected = point; };
    const key = key => fresh._inspectKeyboard({target: chart, key, preventDefault() {}});
    key('Home'); assert.equal(inspected.value, 61);
    key('ArrowRight'); assert.equal(inspected.value, 62);
    assert.match(fresh._graph(plant.attributes, plant.entity_id), /data-value="62"/);
    assert.equal(fresh._inspectionData(h, fresh._historyPoints(h), inspected).date, timestamps[1]);
    key('End'); key('ArrowLeft'); assert.equal(inspected.value, 62);
    h.points = [{state: '59', last_changed: stamp(-1)}, ...h.points.map(point => ({...point}))];
    assert.match(fresh._graph(plant.attributes, plant.entity_id), /data-value="62"/);
    key('ArrowLeft'); assert.equal(inspected.value, 61);
  }
});

console.log('PASS: compact truthful tiles, approximate day labels and selection of real history points');
