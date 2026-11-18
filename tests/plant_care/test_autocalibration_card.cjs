const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let Card;
const context = {
  HTMLElement: class {
    attachShadow() { this.shadowRoot = {innerHTML: '', querySelectorAll: () => []}; }
  },
  customElements: {get: () => null, define: (_name, constructor) => { Card = constructor; }},
  window: {}, location: {origin: 'http://ha.invalid'}, URL, Date, Intl, Set, Number,
};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '../../www/plant-care/plant-care-card.js'), 'utf8'),
  context,
);

const now = 1795000000;
const active = {
  calibration_enabled: true,
  calibration_status: 'awaiting_reference',
  calibration_revision: 0,
  calibration_baseline_dry: 67,
  calibration_baseline_wet: 221,
  calibration_dry_adc: 67,
  calibration_wet_adc: 221,
  calibration_last_change_at: null,
  calibration_observations: 50,
  calibration_references: 0,
  calibration_history_available: true,
  calibration_history: [],
};

function render(calibration = {}, status = 'sensor_error') {
  const card = new Card();
  card._hass = {states: {}};
  return card._plant({
    entity_id: 'sensor.plant',
    state: status,
    attributes: {
      plant_name: 'Стрелиция',
      plant_care: true,
      moisture: 100,
      untrusted_moisture: 100,
      reliability_valid: status === 'ok',
      reliability_reason: status === 'calibrating' ? 'calibrating' : 'saturated',
      last_sample_at: now,
      ...calibration,
    },
  });
}

function event(kind, changes = {}) {
  return {
    event_id: kind,
    at: now - 300,
    detected_at: now - 300,
    kind,
    old_dry: 67,
    old_wet: 221,
    new_dry: 67,
    new_wet: 222,
    reason: null,
    revision: 1,
    ...changes,
  };
}

assert.doesNotMatch(render(), /class="calibration"/);
const awaiting = render(active);
assert.match(awaiting, /class="calibration"/);
assert.match(awaiting, /Автокалибровка/);
assert.match(awaiting, /наблюдение.*независимый эталон/);
// An unavailable or unknown journal is neither an empty journal nor applied evidence.
for (const available of [false, undefined]) {
  for (const history of [[], [event('automatic')]]) {
    const unavailable = render({
      ...active, calibration_history_available: available, calibration_history: history,
    });
    assert.match(unavailable, /История калибровки временно недоступна/);
    assert.doesNotMatch(unavailable, /Записанных изменений калибровки пока нет/);
    assert.equal((unavailable.match(/class="calibration-event"/g) || []).length, 0);
    assert.doesNotMatch(unavailable, /Автоматическая калибровка/);
  }
}

assert.match(awaiting, /Записанных изменений калибровки пока нет/);
assert.match(awaiting, /Недостоверные показания/);
assert.doesNotMatch(awaiting, /100<small> %/);
assert.doesNotMatch(awaiting, /Автоматическая калибровка/);

const settling = render({...active, calibration_status: 'applied', reliability_valid: true}, 'calibrating');
assert.match(settling, /class="plant calibrating"/);
assert.match(settling, /Проверяю.*шкал/);
assert.doesNotMatch(settling, /Недостоверные показания/);
assert.doesNotMatch(settling, /100<small> %/);

const changes = [
  event('automatic', {event_id: 'auto'}),
  event('manual', {event_id: 'manual', at: now - 200, old_wet: 222, new_wet: 223}),
  event('rollback', {event_id: 'rollback', at: now - 100, old_wet: 223, new_wet: 222}),
];
const journal = render({...active, calibration_history: changes});
assert.match(journal, /<details class="calibration-details"/);
assert.match(journal, /class="calibration-history"/);
assert.match(journal, /Автоматическая калибровка/);
assert.match(journal, /Изменение настройки/);
assert.match(journal, /Откат к предыдущей шкале/);
assert.match(journal, /221.*→.*222/);
assert.equal((journal.match(/class="calibration-event"/g) || []).length, 3);

const sameTime = render({...active, calibration_history: [
  event('automatic', {event_id: 'first', revision: 1}),
  event('manual', {event_id: 'last', revision: 2, old_wet: 222, new_wet: 230}),
]});
assert.ok(sameTime.indexOf('Изменение настройки') < sameTime.indexOf('Автоматическая калибровка'));
const concurrent = render({...active, calibration_history: [
  event('automatic', {reason: 'concurrent_change', old_wet: 230}),
]});
assert.match(concurrent, /Автоматическая запись при конфликте настроек/);
assert.match(concurrent, /калибровка не подтверждена/);
assert.doesNotMatch(concurrent, /Автоматическая калибровка/);

const discovered = render({...active, calibration_history: [
  event('external_discovered', {at: null, detected_at: now, new_wet: 224}),
]});
assert.match(discovered, /Обнаружено внешнее изменение/);
assert.match(discovered, /Время изменения неизвестно/);
assert.match(discovered, /data-time-kind="detected"/);
assert.doesNotMatch(discovered, /data-time-kind="changed"/);

const disabled = render({...active, calibration_enabled: false, calibration_history: changes});
assert.match(disabled, /Автокалибровка.*отключена/);
assert.equal((disabled.match(/class="calibration-event"/g) || []).length, 3);
const pending = render({...active, calibration_status: 'applying'});
assert.match(pending, /сохраняю изменение шкалы/i);
assert.doesNotMatch(pending, /Автоматическая калибровка/);

const malformed = render({...active, calibration_history: [
  null, {kind: 'automatic'}, event('<img src=x onerror=alert(1)>'),
]});
assert.doesNotMatch(malformed, /<img src=x/);
assert.doesNotMatch(malformed, /NaN|undefined/);
assert.equal((malformed.match(/class="calibration-event"/g) || []).length, 0);
assert.match(malformed, /Записанных изменений калибровки пока нет/);

const longHistory = Array.from({length: 20}, (_, i) => event('automatic', {
  event_id: String(i), at: now - i * 600, detected_at: now - i * 600,
}));
const complete = render({...active, calibration_history: longHistory});
assert.equal((complete.match(/class="calibration-event"/g) || []).length, 20);
assert.equal(
  (complete.match(/data-action=/g) || []).length,
  (render().match(/data-action=/g) || []).length,
);

const explained = render({...active, calibration_history: [
  event('automatic', {reason: 'qualified_drift'}),
  event('rollback', {reason: 'contradiction'}),
  event('external_discovered', {at: null, detected_at: now, reason: 'pending_effect_reconciled'}),
]});
assert.match(explained, /Независимые опорные измерения подтвердили улучшение/);
assert.match(explained, /прежняя шкала дала меньшую ошибку/);
assert.match(explained, /Проверено по текущим настройкам/);
assert.doesNotMatch(explained, /после перезапуска/);
assert.match(explained, /автор изменения неизвест/);

// Real hass updates and persistent DOM are verified by verify_card_interactions.cjs in CI.
// This bounded test retains independent open/closed state for both plants.
const rerendered = new Card();
const observed = {
  states: Object.fromEntries(['sensor.alpha', 'sensor.beta'].map(entity => [entity, {
    entity_id: entity, state: 'sensor_error',
    attributes: {plant_care: true, plant_name: entity, reliability_valid: false, ...active},
  }])),
};
rerendered._hass = observed;
const sections = [
  {dataset: {entity: 'sensor.alpha'}, open: true},
  {dataset: {entity: 'sensor.beta'}, open: false},
];
rerendered.shadowRoot.querySelectorAll = selector =>
  selector === '.calibration-details[data-entity]' ? sections : [];
rerendered._rememberCalibrationSections();
const alphaTag = rerendered._calibration(active, 'sensor.alpha').match(/<details[^>]*>/)?.[0] || '';
const betaTag = rerendered._calibration(active, 'sensor.beta').match(/<details[^>]*>/)?.[0] || '';
assert.match(alphaTag, /\bopen(?:\s|>)/);
assert.doesNotMatch(betaTag, /\bopen(?:\s|>)/);
sections[0].open = false;
sections[1].open = true;
rerendered._rememberCalibrationSections();
const closedAlpha = rerendered._calibration(active, 'sensor.alpha').match(/<details[^>]*>/)?.[0] || '';
const openedBeta = rerendered._calibration(active, 'sensor.beta').match(/<details[^>]*>/)?.[0] || '';
assert.doesNotMatch(closedAlpha, /\bopen(?:\s|>)/);
assert.match(openedBeta, /\bopen(?:\s|>)/);

console.log('PASS: calibration observation, real audit history, unknown offline time, and quiet settling');
