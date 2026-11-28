// SPDX-License-Identifier: MIT
// Render exact component bytes with sanitized observed API state in native CI only.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {parseArgs} = require('node:util');
const {execFileSync} = require('node:child_process');
const {verifyCardInteractions, verifySyntheticCardInteractions} = require('./verify_card_interactions.cjs');

const ORIGIN = 'http://plant-card-preview.invalid';
const BASELINE_CARD_SHA = '4a9059f2b8e2a3cd139e88e81af12237442fef1aecdc0e9cbf7cb3d16c92fcd3';
const logDirectory = fs.mkdtempSync('/tmp/plant-card-preview-');
fs.chmodSync(logDirectory, 0o700);
const logPath = path.join(logDirectory, 'execution.log');
fs.writeFileSync(logPath, '', {mode: 0o600});
let artifactDirectory;
function log(message) {
  const line = new Date().toISOString() + ' ' + message + '\n';
  process.stdout.write(line);
  fs.appendFileSync(logPath, line);
}
log('Log: ' + logPath);

const allowedAttributes = new Set([
  'plant_care', 'plant_name', 'entry_id', 'source_entity', 'freshness_entity', 'picture', 'picture_revision',
  'moisture', 'raw_moisture', 'last_sample_at', 'last_value_change_at',
  'reliability_valid', 'reliability_reason', 'reliability_incident_started_at',
  'reliability_incident_reason', 'dry_threshold', 'confirmation_hours',
  'last_watering_at', 'last_watering_source', 'last_watering_precision',
  'last_watering_window_start', 'last_watering_window_end', 'last_watering_detected_at',
  'last_watering_retroactive', 'last_watering_note', 'watering_detection_status', 'watering_history',
  'forecast_status', 'forecast_due_at', 'voice_next_at', 'voice_unavailable_reason', 'voice_target_name',
  'wet_risk', 'wet_since', 'snoozed_until', 'due_since',
  'calibration_enabled', 'calibration_status', 'calibration_revision',
  'calibration_baseline_dry', 'calibration_baseline_wet', 'calibration_dry_adc',
  'calibration_wet_adc', 'calibration_source_fingerprint', 'calibration_last_change_at',
  'calibration_observations', 'calibration_references', 'calibration_history',
  'calibration_history_available'
]);
function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}
function verifyCalibrationPayload(attributes) {
  if (!Object.hasOwn(attributes, 'calibration_enabled')) return;
  assert.equal(typeof attributes.calibration_enabled, 'boolean');
  assert.equal(typeof attributes.calibration_status, 'string');
  assert.equal(typeof attributes.calibration_history_available, 'boolean');
  const history = attributes.calibration_history;
  assert.ok(Array.isArray(history) && history.length <= 1000);
  const eventFields = new Set([
    'event_id', 'at', 'detected_at', 'kind', 'old_dry', 'old_wet',
    'new_dry', 'new_wet', 'reason', 'revision'
  ]);
  for (const event of history) {
    assert.ok(event && typeof event === 'object' && !Array.isArray(event));
    for (const key of Object.keys(event)) assert.ok(eventFields.has(key), 'Unexpected calibration history field: ' + key);
    assert.ok(typeof event.event_id === 'string' && event.event_id.length > 0);
    assert.ok([event.old_dry, event.old_wet, event.new_dry, event.new_wet].every(Number.isFinite));
    assert.ok(['automatic', 'rollback', 'manual', 'external_discovered'].includes(event.kind));
    assert.ok(event.at === null || Number.isFinite(event.at));
    assert.ok(Number.isFinite(event.detected_at));
  }
}
function verifyWateringPayload(attributes) {
  if (!Object.hasOwn(attributes, 'watering_history')) return;
  const events = attributes.watering_history;
  assert.ok(Array.isArray(events) && events.length <= 1000);
  const fields = new Set([
    'event_id', 'source', 'precision', 'retroactive', 'note', 'timezone', 'valid',
    'at', 'window_start', 'window_end', 'detected_at', 'retracted_at'
  ]);
  for (const event of events) {
    assert.ok(event && typeof event === 'object' && !Array.isArray(event));
    for (const key of Object.keys(event)) assert.ok(fields.has(key), 'Unexpected watering history field: ' + key);
    assert.ok(typeof event.event_id === 'string' && event.event_id);
    assert.equal(typeof event.valid, 'boolean');
    for (const key of ['at', 'window_start', 'window_end', 'detected_at', 'retracted_at']) {
      assert.ok(event[key] == null || (typeof event[key] === 'string' && Number.isFinite(Date.parse(event[key]))));
    }
  }
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
  for (const key of Object.keys(data.plant.attributes)) {
    assert.ok(allowedAttributes.has(key), 'Snapshot has an unexpected attribute: ' + key);
  }
  verifyCalibrationPayload(data.plant.attributes);
  verifyWateringPayload(data.plant.attributes);
  historyEntry(data);
  return {data, hash: sha256(original)};
}
function historyEntry(snapshot) {
  if (!snapshot.history) return null;
  const history = snapshot.history;
  assert.equal(history.entity_id, snapshot.plant.attributes.source_entity);
  assert.ok(Array.isArray(history.points) && history.points.length <= 2500);
  if (Object.hasOwn(history, 'include_initial_state')) assert.equal(typeof history.include_initial_state, 'boolean');
  const start = Date.parse(history.start);
  const end = Date.parse(history.end);
  assert.ok(Number.isFinite(start) && Number.isFinite(end) && start < end);
  return {points: history.points, start, end, includesInitialState: history.include_initial_state !== false,
    loading: false, fetchedAt: Date.parse(snapshot.observed_at)};
}
function readPhotoManifest(filename) {
  if (!filename) return {hash: null, assets: []};
  const raw = fs.readFileSync(filename);
  const manifest = JSON.parse(raw);
  assert.equal(manifest.schema, 'plant-care-photo-assets/v1');
  assert.equal(manifest.source, 'Home Assistant HTTP capture');
  assert.ok(Array.isArray(manifest.assets) && manifest.assets.length <= 16);
  const directory = path.dirname(path.resolve(filename));
  const assets = manifest.assets.map(asset => {
    assert.ok(typeof asset.file === 'string' && !path.isAbsolute(asset.file));
    const fullPath = path.resolve(directory, asset.file);
    const relative = path.relative(directory, fullPath);
    assert.ok(relative && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative));
    assert.match(asset.sha256, /^[0-9a-f]{64}$/);
    assert.ok(['image/png', 'image/jpeg', 'image/webp'].includes(asset.mime));
    const sourceURL = new URL(asset.source_url, ORIGIN);
    assert.ok(['http:', 'https:'].includes(sourceURL.protocol) && !sourceURL.username && !sourceURL.password);
    assert.ok(Number.isFinite(Date.parse(asset.observed_at)));
    const bytes = fs.readFileSync(fullPath);
    assert.equal(bytes.length, asset.bytes);
    assert.equal(sha256(bytes), asset.sha256, 'Captured photo bytes differ from their manifest');
    return {...asset, bytes};
  });
  assert.equal(new Set(assets.map(asset => asset.source_url)).size, assets.length);
  return {hash: sha256(raw), assets};
}
function photoForSnapshot(snapshot, manifest) {
  const picture = snapshot.plant.attributes.picture;
  if (!picture) return null;
  const asset = manifest.assets.find(item => item.source_url === picture);
  assert.ok(asset, 'Observed photo has no exact captured asset; supply --photo-manifest');
  const originalURL = new URL(picture, ORIGIN);
  const revisedURL = new URL(originalURL);
  if (snapshot.plant.attributes.picture_revision && revisedURL.origin === ORIGIN) {
    revisedURL.searchParams.set('plant_care_revision', snapshot.plant.attributes.picture_revision);
  }
  return {...asset, allowedURLs: new Set([originalURL.href, revisedURL.href])};
}
function pageHTML(theme) {
  const dark = theme === 'dark';
  const colors = dark
    ? {background: '#111714', text: '#e4ede8', muted: '#aab8ae', card: '#1b2420', line: '#3f4e45', accent: '#79c596', error: '#ffad96'}
    : {background: '#f6f7f8', text: '#243029', muted: '#66736b', card: '#ffffff', line: '#dce2de', accent: '#39785b', error: '#a63c27'};
  return '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + '<style>body{margin:0;padding:20px;background:' + colors.background + ';color:' + colors.text + ';font-family:Arial,sans-serif}'
    + 'main{max-width:720px;margin:0 auto}h1{font-size:16px;margin:0 0 8px}p{font-size:11px;line-height:1.45;color:' + colors.muted + ';overflow-wrap:anywhere}'
    + 'plant-care-card{display:block;--primary-color:' + colors.accent + ';--primary-text-color:' + colors.text + ';--secondary-text-color:' + colors.muted + ';'
    + '--ha-card-background:' + colors.card + ';--divider-color:' + colors.line + ';--error-color:' + colors.error + '}'
    + '</style></head><body><main><h1>Растение · проверка компонента</h1><p id="receipt"></p><plant-care-card></plant-care-card>'
    + '<p>Компонент на сохранённых данных Home Assistant. Фото — точные HTTP-байты по наблюдаемому URL;'
    + ' история — записи Recorder. Это проверка компонента, не снимок самого дашборда.'
    + ' Относительное время зафиксировано на момент получения данных.</p></main></body></html>';
}
async function mount(page, snapshot, cardPath, cardHash, theme, photo, legacy) {
  const network = {photoRequests: 0, blocked: []};
  await page.clock.setFixedTime(new Date(snapshot.observed_at));
  assert.equal(await page.evaluate(() => Date.now()), Date.parse(snapshot.observed_at));
  await page.route('**/*', route => {
    const url = route.request().url();
    if (url === ORIGIN + '/') return route.fulfill({status: 200, contentType: 'text/html; charset=utf-8', body: pageHTML(theme)});
    if (photo?.allowedURLs.has(url)) {
      network.photoRequests++;
      return route.fulfill({status: 200, contentType: photo.mime, body: photo.bytes});
    }
    network.blocked.push(url);
    return route.abort();
  });
  await page.goto(ORIGIN + '/', {waitUntil: 'domcontentloaded'});
  assert.equal(await page.evaluate(() => Date.now()), Date.parse(snapshot.observed_at), 'Capture clock must survive navigation');
  await page.addScriptTag({path: cardPath});
  await page.evaluate(({snapshot, recordedHistory, cardHash}) => {
    document.querySelector('#receipt').textContent =
      'Данные: ' + snapshot.observed_at + ' · SHA-256 карточки: ' + cardHash;
    const card = document.querySelector('plant-care-card');
    card.setConfig({entities: [snapshot.plant.entity_id]});
    if (recordedHistory) card._history[snapshot.plant.attributes.source_entity + '|7'] = recordedHistory;
    card.hass = {states: {
      [snapshot.plant.entity_id]: snapshot.plant,
      'input_boolean.plant_care_voice_enabled': {state: snapshot.voice_enabled ? 'on' : 'off'}
    }};
    const style = document.createElement('style');
    style.textContent = 'ha-card{display:block}';
    card.shadowRoot.appendChild(style);
  }, {snapshot, recordedHistory: historyEntry(snapshot), cardHash});
  await page.locator('plant-care-card ' + (legacy ? '.plant' : '.plant-tile')).waitFor();
  return network;
}
async function verifyFault(page, snapshot) {
  if (snapshot.plant.state !== 'sensor_error') return;
  assert.equal(await page.locator('plant-care-card .plant .status').innerText(), 'Недостоверные показания');
  assert.equal(await page.locator('plant-care-card .plant .moisture').innerText(), '—');
  assert.match(await page.locator('plant-care-card .plant .notice').innerText(), /Полив по этим данным не определяется/);
}
async function verifyCalibrationView(page, snapshot) {
  const attributes = snapshot.plant.attributes;
  if (typeof attributes.calibration_enabled !== 'boolean') return {observed: false, opened: false, preservedAfterUpdate: false};
  assert.ok(await page.locator('plant-care-card .calibration-status').isVisible());
  const details = page.locator('plant-care-card .calibration-details');
  if (!await details.evaluate(element => element.open)) await details.locator('summary').click();
  assert.ok(await details.evaluate(element => element.open));
  await page.evaluate(() => {
    const card = document.querySelector('plant-care-card');
    card.hass = card.hass;
  });
  const preservedAfterUpdate = await details.evaluate(element => element.open);
  assert.ok(preservedAfterUpdate, 'An ordinary HA update closed the calibration history');
  assert.ok(await page.locator('plant-care-card .calibration-scale').isVisible());
  if (!attributes.calibration_history_available) {
    assert.equal(await page.locator('plant-care-card .calibration-event').count(), 0);
    assert.equal(await page.locator('plant-care-card .calibration-unavailable').innerText(), 'История калибровки временно недоступна');
    assert.equal(await page.locator('plant-care-card .calibration-empty').count(), 0);
  } else {
    assert.equal(await page.locator('plant-care-card .calibration-event').count(), attributes.calibration_history.length);
    if (!attributes.calibration_history.length) {
      assert.equal(await page.locator('plant-care-card .calibration-empty').innerText(), 'Записанных изменений калибровки пока нет');
    }
  }
  return {observed: true, opened: true, preservedAfterUpdate};
}
async function verifyPhotoLoaded(page, photo, legacy) {
  if (!photo) return;
  const selector = legacy ? '.plant img.photo' : '.plant-tile img.photo, dialog[open] img.photo';
  await page.waitForFunction(selector => {
    const images = [...document.querySelector('plant-care-card').shadowRoot.querySelectorAll(selector)];
    return images.length > 0 && images.every(image => image.complete && image.naturalWidth > 0);
  }, selector);
}
async function capture(page, output, file, metadata) {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Component has horizontal overflow');
  const openDialog = page.locator('plant-care-card dialog[open]');
  if (await openDialog.count()) {
    assert.ok(await openDialog.evaluate(dialog => dialog.scrollWidth <= dialog.clientWidth + 1), 'Detail dialog has horizontal overflow');
  }
  await page.screenshot({path: path.join(output, file), fullPage: true});
  log('Captured ' + file);
  return {file, sha256: sha256(fs.readFileSync(path.join(output, file))), ...metadata};
}
function archiveInteractionLog(report, output, filename) {
  if (!report.logPath) return report;
  fs.copyFileSync(report.logPath, path.join(output, filename));
  return {...report, logPath: filename};
}
async function runSynthetic(browser, viewport, snapshot, cardPath, output) {
  const page = await browser.newPage({viewport: {width: viewport.width, height: 1000},
    hasTouch: viewport.touch, locale: 'ru-RU', timezoneId: snapshot.time_zone});
  page.setDefaultTimeout(15000);
  try {
    const harnessHTML = '<!doctype html><html lang="ru"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
      + '<style>body{margin:20px;font-family:Arial,sans-serif}plant-care-card{display:block;max-width:720px}</style>'
      + '</head><body><h1>Synthetic interaction harness — no screenshots</h1></body></html>';
    await page.route('**/*', route => route.request().url() === ORIGIN + '/synthetic'
      ? route.fulfill({status: 200, contentType: 'text/html; charset=utf-8', body: harnessHTML})
      : route.abort());
    await page.goto(ORIGIN + '/synthetic', {waitUntil: 'domcontentloaded'});
    await page.addScriptTag({path: cardPath});
    const report = await verifySyntheticCardInteractions(page);
    return archiveInteractionLog(report, output, 'synthetic-' + viewport.name + '.log');
  } catch (error) {
    if (error.interactionLogPath) fs.copyFileSync(error.interactionLogPath, path.join(output, 'synthetic-' + viewport.name + '-failed.log'));
    throw error;
  } finally {
    await page.close();
  }
}
async function captureDetailSections(page, snapshot, photo, legacy, output, name, viewport, theme) {
  const screenshots = [];
  await verifyFault(page, snapshot);
  await verifyPhotoLoaded(page, photo, legacy);
  if (!legacy) await page.locator('plant-care-card dialog.plant-dialog').evaluate(dialog => { dialog.scrollTop = 0; });
  screenshots.push(await capture(page, output, name + '-detail.png', {viewport: viewport.name, theme, view: 'detail', synthetic: false}));
  const calibrationDetails = await verifyCalibrationView(page, snapshot);
  if (calibrationDetails.opened) {
    await page.locator('plant-care-card .calibration-details').scrollIntoViewIfNeeded();
    screenshots.push(await capture(page, output, name + '-calibration.png',
      {viewport: viewport.name, theme, view: 'calibration', synthetic: false, calibrationDetails}));
  }
  const watering = page.locator('plant-care-card .watering-details');
  if (!legacy && await watering.count()) {
    if (!await watering.evaluate(details => details.open)) await watering.locator('summary').click();
    await watering.scrollIntoViewIfNeeded();
    screenshots.push(await capture(page, output, name + '-watering.png',
      {viewport: viewport.name, theme, view: 'watering-history', synthetic: false}));
  }
  return screenshots;
}
async function renderPage(browser, snapshot, cardPath, cardHash, output, photo, legacy, viewport, theme) {
  const name = viewport.name + '-' + theme;
  const page = await browser.newPage({viewport: {width: viewport.width, height: 1000}, hasTouch: viewport.touch,
    locale: 'ru-RU', timezoneId: snapshot.time_zone, colorScheme: theme});
  page.setDefaultTimeout(15000);
  const errors = [];
  const screenshots = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    const network = await mount(page, snapshot, cardPath, cardHash, theme, photo, legacy);
    let interaction;
    if (legacy) {
      interaction = {view: name, status: 'not-applicable-original-card', reason: 'Exact original card has no compact tiles or interactive chart'};
    } else {
      const report = await verifyCardInteractions(page, snapshot);
      interaction = {view: name, ...archiveInteractionLog(report, output, 'interactions-' + name + '.log')};
      screenshots.push(await capture(page, output, name + '-compact.png', {viewport: viewport.name, theme, view: 'compact', synthetic: false}));
      await page.locator('plant-care-card .plant-tile').click();
      await page.locator('plant-care-card dialog.plant-dialog').waitFor({state: 'visible'});
    }
    screenshots.push(...await captureDetailSections(page, snapshot, photo, legacy, output, name, viewport, theme));
    assert.deepEqual(errors, [], 'The component produced a browser exception');
    assert.deepEqual(network.blocked.filter(url => !url.endsWith('/favicon.ico')), [], 'Unexpected network request in an offline component preview');
    if (photo) assert.ok(network.photoRequests > 0, 'Captured photo bytes were never served to the browser');
    return {screenshots, interaction};
  } catch (error) {
    if (error.interactionLogPath) fs.copyFileSync(error.interactionLogPath, path.join(output, 'interactions-' + name + '-failed.log'));
    throw error;
  } finally {
    await page.close();
  }
}
async function renderViews(browser, snapshot, cardPath, cardHash, output, photo, legacy) {
  const screenshots = [];
  const interactions = [];
  const synthetic = [];
  for (const viewport of [{name: 'desktop', width: 900, touch: false}, {name: 'mobile', width: 390, touch: true}]) {
    for (const theme of ['light', 'dark']) {
      const view = await renderPage(browser, snapshot, cardPath, cardHash, output, photo, legacy, viewport, theme);
      screenshots.push(...view.screenshots);
      interactions.push(view.interaction);
    }
    if (!legacy) synthetic.push({viewport: viewport.name, ...await runSynthetic(browser, viewport, snapshot, cardPath, output)});
  }
  return {screenshots, interactions, synthetic};
}
function cardSource(revision, output) {
  const relativePath = 'www/plant-care/plant-care-card.js';
  if (!revision) return {path: path.resolve(relativePath), commit: execFileSync('git', ['rev-parse', 'HEAD'], {encoding: 'utf8'}).trim()};
  assert.match(revision, /^[0-9a-f]{40}$/, 'Historical card source requires an immutable commit');
  const commit = execFileSync('git', ['rev-parse', '--verify', revision + '^{commit}'], {encoding: 'utf8'}).trim();
  const content = execFileSync('git', ['show', commit + ':' + relativePath]);
  const filename = path.join(output, 'rendered-card.js');
  fs.writeFileSync(filename, content, {mode: 0o600});
  return {path: filename, commit};
}
async function main() {
  const {values} = parseArgs({options: {
    snapshot: {type: 'string'}, output: {type: 'string'},
    'card-revision': {type: 'string'}, 'expected-card-sha': {type: 'string'},
    'photo-manifest': {type: 'string'}, 'validate-only': {type: 'boolean', default: false}
  }});
  assert.ok(values.snapshot && values.output, '--snapshot and --output are required');
  const {data: snapshot, hash: snapshotHash} = readSnapshot(values.snapshot);
  const manifest = readPhotoManifest(values['photo-manifest']);
  const photo = photoForSnapshot(snapshot, manifest);
  const output = path.resolve(values.output);
  artifactDirectory = output;
  fs.mkdirSync(output, {recursive: true});
  const source = cardSource(values['card-revision'], output);
  const cardHash = sha256(fs.readFileSync(source.path));
  if (values['expected-card-sha']) {
    assert.match(values['expected-card-sha'], /^[0-9a-f]{64}$/);
    assert.equal(cardHash, values['expected-card-sha'], 'Card bytes differ from the captured baseline');
  }
  const receipt = {
    kind: values['validate-only'] ? 'component-input-validation-only' : 'component-preview-from-live-api-snapshot',
    live_dashboard_screenshot: false, observed_at: snapshot.observed_at, time_zone: snapshot.time_zone,
    evaluated_at: snapshot.observed_at, clock: 'Playwright setFixedTime at observed_at',
    snapshot_sha256: snapshotHash, card_sha256: cardHash, card_git_commit: source.commit,
    git_commit: execFileSync('git', ['rev-parse', 'HEAD'], {encoding: 'utf8'}).trim(),
    plant_entity: snapshot.plant.entity_id, plant_state: snapshot.plant.state,
    history_included: Boolean(snapshot.history), photos_included: Boolean(photo),
    photo_manifest_sha256: manifest.hash,
    photo: photo ? {source_url: photo.source_url, sha256: photo.sha256, bytes: photo.bytes.length,
      mime: photo.mime, observed_at: photo.observed_at, inspection: photo.inspection} : null,
    calibration_fields_observed: typeof snapshot.plant.attributes.calibration_enabled === 'boolean',
    watering_history_observed: Array.isArray(snapshot.plant.attributes.watering_history),
    screenshot_data_mutated: false, renderer_origin: ORIGIN,
  };
  log('Component ' + cardHash + ' capture ' + snapshot.observed_at + ' photo=' + (photo?.sha256 || 'absent'));
  if (values['validate-only']) {
    fs.writeFileSync(path.join(output, 'validation.json'), JSON.stringify({...receipt, browser_launched: false}, null, 2) + '\n');
    log('Input validation completed rc=0; no browser launched. Log: ' + logPath);
    fs.copyFileSync(logPath, path.join(output, 'execution.log'));
    return;
  }
  const {chromium} = require('playwright');
  const browser = await chromium.launch({headless: true, timeout: 30000});
  try {
    const views = await renderViews(browser, snapshot, source.path, cardHash, output, photo, cardHash === BASELINE_CARD_SHA);
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({...receipt,
      rendered_at: new Date().toISOString(), ...views}, null, 2) + '\n');
    log('Completed component preview rc=0. Output: ' + output + '. Log: ' + logPath);
  } finally {
    await browser.close();
    fs.copyFileSync(logPath, path.join(output, 'execution.log'));
  }
}
main().catch(error => {
  log('Failed rc=1: ' + error.stack + '. Log: ' + logPath);
  if (artifactDirectory) fs.copyFileSync(logPath, path.join(artifactDirectory, 'execution.log'));
  process.exitCode = 1;
});
