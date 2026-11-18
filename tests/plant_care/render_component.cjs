// SPDX-License-Identifier: MIT
// Render the actual component in native CI using a sanitized production snapshot.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {parseArgs} = require('node:util');
const {execFileSync} = require('node:child_process');

const logDirectory = fs.mkdtempSync('/tmp/plant-card-preview-');
fs.chmodSync(logDirectory, 0o700);
const logPath = path.join(logDirectory, 'execution.log');
fs.writeFileSync(logPath, '', {mode: 0o600});
function log(message) {
  const line = new Date().toISOString() + ' ' + message + '\n';
  process.stdout.write(line);
  fs.appendFileSync(logPath, line);
}
log('Log: ' + logPath);

const allowedAttributes = new Set([
  'plant_care', 'plant_name', 'entry_id', 'source_entity', 'freshness_entity',
  'moisture', 'raw_moisture', 'last_sample_at', 'last_value_change_at',
  'reliability_valid', 'reliability_reason', 'reliability_incident_started_at',
  'reliability_incident_reason', 'dry_threshold', 'confirmation_hours',
  'last_watering_at', 'last_watering_source', 'forecast_status', 'forecast_due_at',
  'voice_next_at', 'voice_unavailable_reason', 'voice_target_name',
  'wet_risk', 'wet_since', 'snoozed_until', 'due_since'
]);
function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}
function readSnapshot(filename) {
  const original = fs.readFileSync(filename);
  const data = JSON.parse(original);
  assert.equal(data.schema, 'plant-care-live-snapshot/v1');
  assert.equal(data.source, 'Home Assistant REST API');
  const observed = Date.parse(data.observed_at);
  assert.ok(Number.isFinite(observed) && observed <= Date.now() + 60000, 'Invalid capture time');
  assert.equal(typeof data.voice_enabled, 'boolean', 'The voice toggle must be observed');
  assert.equal(typeof data.time_zone, 'string', 'The Home Assistant time zone must be captured');
  new Intl.DateTimeFormat('ru-RU', {timeZone: data.time_zone}).format(new Date(observed));
  assert.match(data.plant.entity_id, /^sensor\.[a-z0-9_]+$/);
  assert.equal(data.plant.attributes.plant_care, true);
  const attributes = {};
  for (const [key, value] of Object.entries(data.plant.attributes)) {
    assert.ok(allowedAttributes.has(key), 'Snapshot has an unexpected attribute: ' + key);
    attributes[key] = value;
  }
  return {data: {...data, plant: {...data.plant, attributes}}, hash: sha256(original)};
}
function historyEntry(snapshot) {
  if (!snapshot.history) return null;
  const history = snapshot.history;
  assert.equal(history.entity_id, snapshot.plant.attributes.source_entity);
  assert.ok(Array.isArray(history.points) && history.points.length <= 2500);
  const start = Date.parse(history.start);
  const end = Date.parse(history.end);
  assert.ok(Number.isFinite(start) && Number.isFinite(end) && start < end);
  return {points: history.points, start, end, loading: false, fetchedAt: Date.parse(snapshot.observed_at)};
}
function pageHTML() {
  return '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + '<style>body{margin:0;padding:20px;background:#f6f7f8;color:#243029;font-family:Arial,sans-serif}'
    + 'main{max-width:720px;margin:0 auto}h1{font-size:17px;margin:0 0 8px}p{font-size:12px;line-height:1.45;color:#59636d;overflow-wrap:anywhere}'
    + 'plant-care-card{display:block;--primary-color:#39785b;--primary-text-color:#243029;--secondary-text-color:#667;'
    + '--ha-card-background:#fff;--divider-color:#ddd;--error-color:#a63c27}'
    + '</style></head><body><main><h1>Plant card — component preview</h1>'
    + '<p id="receipt"></p><plant-care-card></plant-care-card>'
    + '<p>Rendered from the observed single-plant API snapshot. This is a component preview,'
    + ' not a screenshot of the live Home Assistant dashboard. Photos are omitted;'
    + ' history is shown only when it was captured from Recorder. Relative times are evaluated'
    + ' at the capture timestamp above.</p></main></body></html>';
}
async function mount(page, snapshot, cardPath, cardHash) {
  await page.clock.setFixedTime(new Date(snapshot.observed_at));
  assert.equal(await page.evaluate(() => Date.now()), Date.parse(snapshot.observed_at));
  await page.route('**/*', route => route.abort());
  await page.setContent(pageHTML(), {waitUntil: 'domcontentloaded'});
  await page.addScriptTag({path: cardPath});
  await page.evaluate(({snapshot, recordedHistory, cardHash}) => {
    document.querySelector('#receipt').textContent =
      'Observed and evaluated as of ' + snapshot.observed_at + ' · card SHA-256 ' + cardHash;
    const card = document.querySelector('plant-care-card');
    card.setConfig({entities: [snapshot.plant.entity_id]});
    card.hass = {states: {
      [snapshot.plant.entity_id]: snapshot.plant,
      'input_boolean.plant_care_voice_enabled': {state: snapshot.voice_enabled ? 'on' : 'off'}
    }};
    if (recordedHistory) {
      card._history[snapshot.plant.attributes.source_entity + '|7'] = recordedHistory;
      card._render();
    }
    const style = document.createElement('style');
    style.textContent = 'ha-card{display:block}';
    card.shadowRoot.appendChild(style);
  }, {snapshot, recordedHistory: historyEntry(snapshot), cardHash});
  await page.locator('plant-care-card .plant').waitFor();
}
async function verifyFault(page, snapshot) {
  if (snapshot.plant.state !== 'sensor_error') return;
  assert.equal(await page.locator('plant-care-card .status').innerText(), 'Недостоверные показания');
  assert.equal(await page.locator('plant-care-card .moisture').innerText(), '—');
  assert.match(await page.locator('plant-care-card .notice').innerText(), /Полив по этим данным не определяется/);
}
async function renderViews(browser, snapshot, cardPath, cardHash, output) {
  const screenshots = [];
  for (const viewport of [{name: 'desktop', width: 900}, {name: 'mobile', width: 390}]) {
    const page = await browser.newPage({viewport: {width: viewport.width, height: 900}, locale: 'ru-RU', timezoneId: snapshot.time_zone});
    page.setDefaultTimeout(15000);
    try {
      await mount(page, snapshot, cardPath, cardHash);
      await verifyFault(page, snapshot);
      const file = viewport.name + '.png';
      await page.screenshot({path: path.join(output, file), fullPage: true});
      screenshots.push({file, sha256: sha256(fs.readFileSync(path.join(output, file)))});
      log('Captured ' + viewport.name + ' component preview');
    } finally {
      await page.close();
    }
  }
  return screenshots;
}
async function main() {
  const {values} = parseArgs({options: {snapshot: {type: 'string'}, output: {type: 'string'}}});
  assert.ok(values.snapshot && values.output, '--snapshot and --output are required');
  const {data: snapshot, hash: snapshotHash} = readSnapshot(values.snapshot);
  const output = path.resolve(values.output);
  fs.mkdirSync(output, {recursive: true});
  const cardPath = path.resolve('www/plant-care/plant-care-card.js');
  const cardHash = sha256(fs.readFileSync(cardPath));
  log('Rendering actual component ' + cardHash + ' from capture ' + snapshot.observed_at);
  const {chromium} = require('playwright');
  const browser = await chromium.launch({headless: true, timeout: 30000});
  try {
    const screenshots = await renderViews(browser, snapshot, cardPath, cardHash, output);
    const receipt = {
      kind: 'component-preview-from-live-api-snapshot',
      live_dashboard_screenshot: false,
      observed_at: snapshot.observed_at,
      time_zone: snapshot.time_zone,
      evaluated_at: snapshot.observed_at,
      clock: 'Playwright setFixedTime at observed_at',
      rendered_at: new Date().toISOString(),
      snapshot_sha256: snapshotHash,
      card_sha256: cardHash,
      git_commit: execFileSync('git', ['rev-parse', 'HEAD'], {encoding: 'utf8'}).trim(),
      plant_entity: snapshot.plant.entity_id,
      plant_state: snapshot.plant.state,
      history_included: Boolean(snapshot.history),
      photos_included: false,
      screenshots
    };
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
    log('Completed component preview. Output: ' + output + '. Log: ' + logPath);
  } finally {
    await browser.close();
    fs.copyFileSync(logPath, path.join(output, 'execution.log'));
  }
}
main().catch(error => {
  log('Failed: ' + error.stack + '. Log: ' + logPath);
  process.exitCode = 1;
});
