// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
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
  start, end: start + 3600000, loading: false, fetchedAt: start,
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
console.log('PASS: compact truthful tiles, approximate day labels and selection of real history points');
