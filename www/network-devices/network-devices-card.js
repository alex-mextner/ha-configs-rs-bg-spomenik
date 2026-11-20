/* Network devices: router-cli inventory through authenticated HA services (rest_command.network_*).
 * The inventory is fetched over the HA websocket with return_response — never via /local.
 * Everything coming from devices (hostnames, page titles, vendors, evidence) is rendered with
 * textContent only.
 *
 * Router traffic: the auto-refresh only re-reads the LOCAL inventory (rest_command.network_inventory).
 * The router itself is polled hourly by a systemd timer, and by the "Обновить" button
 * (rest_command.network_update → router inventory update), which the bridge serializes.
 *
 * Performance: the periodic refresh asks for favicons=false; favicons are fetched separately when the
 * browser is idle (every 30 min), deduplicated into blob: URLs and lazy-loaded. Rows are keyed by MAC
 * and rebuilt only when their content changes; long lists are paged. Per-device history (sparklines)
 * is fetched lazily for rows that scroll into view, two requests at a time, cached for 10 minutes.
 *
 * Optional services (degrade gracefully when absent): rest_command.network_stats {days} → header
 * activity summary; rest_command.network_history {device, days} → sparklines and the detail drawer.
 *
 * Copy: IP, MAC, names, vendor, hostnames and service URLs copy on click (execCommand fallback: HA is
 *   often served over plain http, where navigator.clipboard does not exist). All text stays selectable.
 * Pin/unpin/rename update the row at once (optimistic) and re-read the inventory; the bridge refreshes
 *   router-cli's DB after a write. When the router's static-lease slots are full, a picker offers to
 *   replace one reservation (rest_command.network_pin_replace; the bridge rolls back on failure).
 *
 * Config: title, default_filter (recent|active|all|pinned|new|unknown|wifi|wired|gear),
 *         refresh_interval (s, default 60), recent_hours (24), new_hours (24), history_days (7),
 *         dry_run (bool: pin/unpin pass --dry-run to router-cli),
 *         mock (bool: synthesize the new inventory/history/stats fields locally, for development).
 */
const ND_FILTERS = [
  ['recent', 'Недавние'], ['active', 'Онлайн'], ['all', 'Все'],
  ['pinned', 'Закреплённые'], ['new', 'Новые'], ['unknown', 'Неизвестный вендор'],
  ['wifi', 'Wi‑Fi', 'mdi:wifi'], ['wired', 'Кабель', 'mdi:ethernet'], ['gear', 'Сетевое оборудование', 'mdi:router-wireless'],
];
const ND_SORTS = [['last_seen', 'По активности'], ['name', 'По имени'], ['ip', 'По IP'], ['traffic', 'По трафику']];
const ND_MDI = /^mdi:[a-z0-9-]{1,64}$/;
const ND_PAGE = 60;
const ND_HISTORY_TTL = 600e3;
const ND_FAVICON_TTL = 1800e3;
const ND_ICON_RULES = [
  [/iphone|android|galaxy|pixel|redmi|xiaomi.*phone|oneplus|honor|huawei.*p\d|смартфон|телефон/i, 'mdi:cellphone'],
  [/ipad|tablet|tab[ -]?s\d|планшет/i, 'mdi:tablet'],
  [/macbook|laptop|notebook|thinkpad|probook|elitebook|ноутбук/i, 'mdi:laptop'],
  [/desktop|imac|mac-?mini|-pc\b|\bpc\b|workstation/i, 'mdi:desktop-tower-monitor'],
  [/home ?assistant|\bhass\b/i, 'mdi:home-assistant'],
  [/router|gateway|ubee|keenetic|mikrotik|tp-?link|asus.*rt|роутер/i, 'mdi:router-network'],
  [/tv|bravia|webos|tizen|android ?tv|chromecast|телевизор/i, 'mdi:television'],
  [/yandex|станци|station|alexa|echo|homepod|nest ?(mini|audio)|speaker|sonos/i, 'mdi:speaker'],
  [/printer|laserjet|deskjet|epson|brother|canon|kyocera|принтер/i, 'mdi:printer'],
  [/k1|creality|snapmaker|prusa|bambu|klipper|octoprint|fluidd|mainsail|3d/i, 'mdi:printer-3d'],
  [/camera|cam\b|ipc|hikvision|dahua|reolink|камера/i, 'mdi:cctv'],
  [/playstation|xbox|nintendo|switch|steam ?deck/i, 'mdi:gamepad-variant'],
  [/watch|band|часы/i, 'mdi:watch'],
  [/vacuum|roborock|dreame|пылесос/i, 'mdi:robot-vacuum'],
  [/esp|espressif|tuya|shelly|sonoff|tasmota|zigbee|bridge/i, 'mdi:chip'],
  [/nas|synology|qnap/i, 'mdi:nas'],
];
// Category ids from router-cli → label + default icon. Unknown ids are shown as-is.
const ND_CATS = {
  phone: ['Телефон', 'mdi:cellphone'], tablet: ['Планшет', 'mdi:tablet'], watch: ['Часы', 'mdi:watch-variant'],
  laptop: ['Ноутбук', 'mdi:laptop'], desktop: ['Компьютер', 'mdi:desktop-tower-monitor'], computer: ['Компьютер', 'mdi:desktop-tower-monitor'],
  tv: ['ТВ и медиа', 'mdi:television'], media: ['ТВ и медиа', 'mdi:television-box'], speaker: ['Колонка', 'mdi:speaker'],
  printer: ['Принтер', 'mdi:printer'], printer_3d: ['3D‑принтер', 'mdi:printer-3d'], '3d_printer': ['3D‑принтер', 'mdi:printer-3d'],
  router: ['Роутер', 'mdi:router-wireless'], mesh: ['Mesh‑узел', 'mdi:access-point-network'], mesh_node: ['Mesh‑узел', 'mdi:access-point-network'],
  access_point: ['Точка доступа', 'mdi:access-point'], ap: ['Точка доступа', 'mdi:access-point'], switch: ['Коммутатор', 'mdi:switch'],
  network: ['Сетевое оборудование', 'mdi:router-network'],
  iot: ['Умный дом', 'mdi:home-automation'], plug: ['Розетка', 'mdi:power-socket-eu'], light: ['Свет', 'mdi:lightbulb'],
  sensor: ['Датчик', 'mdi:motion-sensor'], ir_remote: ['ИК‑пульт', 'mdi:remote'], esp: ['Микроконтроллер', 'mdi:chip'],
  // router-cli ids (hyphens are normalized to underscores before lookup)
  iot_plug: ['Розетка / реле', 'mdi:power-socket-eu'], iot_light: ['Свет', 'mdi:lightbulb'], iot_sensor: ['Датчик', 'mdi:motion-sensor'],
  esp_diy: ['ESP / DIY', 'mdi:chip'], raspberry_pi: ['Raspberry Pi', 'mdi:raspberry-pi'], media_player: ['Медиаплеер', 'mdi:cast'],
  game_console: ['Консоль', 'mdi:gamepad-variant'],
  camera: ['Камера', 'mdi:cctv'], console: ['Консоль', 'mdi:gamepad-variant'], sbc: ['Одноплатник', 'mdi:raspberry-pi'],
  raspberry: ['Одноплатник', 'mdi:raspberry-pi'], nas: ['NAS', 'mdi:nas'], server: ['Сервер', 'mdi:server'],
  vacuum: ['Пылесос', 'mdi:robot-vacuum'], appliance: ['Бытовая техника', 'mdi:washing-machine'], unknown: ['Неизвестно', 'mdi:help-network'],
};
const ND_GALLERY = [
  ['Телефоны и часы', 'cellphone cellphone-basic cellphone-wireless apple android tablet tablet-cellphone watch watch-variant'],
  ['Компьютеры', 'laptop laptop-account desktop-tower-monitor desktop-tower desktop-classic monitor microsoft-windows linux keyboard'],
  ['ТВ и медиа', 'television television-classic television-box set-top-box cast cast-variant projector audio-video kodi plex'],
  ['Колонки и звук', 'speaker speaker-wireless speaker-multiple soundbar headphones microphone'],
  ['Принтеры', 'printer printer-wireless printer-3d printer-3d-nozzle scanner fax'],
  ['Сеть', 'router-wireless router-network router access-point access-point-network wifi lan switch ethernet network antenna'],
  ['Умный дом', 'home-automation power-socket-eu power-plug lightbulb lightbulb-group led-strip-variant thermometer motion-sensor remote remote-tv air-conditioner air-purifier fan radiator robot-vacuum washing-machine fridge kettle blinds doorbell lock water'],
  ['ESP и платы', 'chip memory developer-board raspberry-pi zigbee bluetooth usb expansion-card'],
  ['Камеры', 'cctv camera webcam doorbell-video'],
  ['Консоли', 'gamepad-variant controller microsoft-xbox sony-playstation nintendo-switch steam'],
  ['Серверы и NAS', 'server server-network nas database harddisk docker home-assistant cloud'],
  ['Прочее', 'car car-electric battery solar-power devices incognito help-network'],
].map(([t, s]) => [t, s.split(' ').map(n => `mdi:${n}`)]);
const ND_WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
// router-cli started recording presence sweeps here (older hours have no data, not "offline").
const ND_PRESENCE_SINCE = Date.parse('2026-09-25T16:05:00Z');
let ndIconList = null;   // MDI names from HA's /static/mdi/iconList.json, loaded on first gallery search

class NetworkDevicesCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    this._data = null; this._error = ''; this._busy = false; this._loadedAt = 0; this._seq = 0;
    this._filter = null; this._query = ''; this._sort = 'last_seen'; this._cat = '';
    this._group = this._pref('group') === '1';
    this._confirm = null;   // {mac, action} of an open pin/unpin confirmation
    this._editing = null;   // mac with an open rename/icon editor
    this._pending = new Set(); // macs with an action in flight
    this._open = new Set();    // macs with the "why" details expanded
    this._rows = new Map();    // key → {sig, el}
    this._shown = ND_PAGE;
    this._favicons = new Map(); this._faviconBlobs = new Map(); this._faviconsAt = 0;
    this._history = new Map(); this._histQueue = []; this._histActive = 0;
    this._stats = null; this._statsAt = 0;
    this._missing = new Map();   // service → when the bridge said not_found
    this._drawerMac = null;
    this._perf = {};   // timings for debugging: hass handed over, inventory call, list render (ms)
    this._overrides = new Map();  // mac → {patch, until}: optimistic state after pin/unpin/alias
    this._visibility = () => { if (!document.hidden && Date.now() - this._loadedAt > 15000) this._load(); };
    this._keydown = (e) => { if (e.key === 'Escape' && this._drawerMac) this._closeDrawer(); };
  }

  setConfig(config) {
    this._config = {refresh_interval: 60, recent_hours: 24, new_hours: 24, history_days: 7, ...(config || {})};
    if (this._filter === null) {
      const f = this._config.default_filter;
      this._filter = ND_FILTERS.some(([k]) => k === f) ? f : 'recent';
    }
    if (this._built) { this._rows.clear(); this._render(); }
  }
  set hass(value) {
    const first = !this._hass; this._hass = value;
    if (first) this._perf.hass = Math.round(performance.now());
    if (first && this.isConnected) this._start();
  }
  getCardSize() { return 10; }
  getGridOptions() { return {columns: 'full', rows: 'auto'}; }
  connectedCallback() { if (!this._built) this._build(); this._start(); }
  disconnectedCallback() {
    clearInterval(this._timer); this._timer = null; clearTimeout(this._scanTimer);
    document.removeEventListener('visibilitychange', this._visibility);
    document.removeEventListener('keydown', this._keydown);
    this._io?.disconnect();
    this._gal?.close(false);
  }

  _start() {
    if (this._timer || !this._hass || !this.isConnected) return;
    if (!this._built) this._build();
    document.addEventListener('visibilitychange', this._visibility);
    document.addEventListener('keydown', this._keydown);
    const every = Math.max(15, Number(this._config?.refresh_interval) || 60) * 1000;
    this._timer = setInterval(() => { if (!document.hidden) this._load(); }, every);
    this._load();
  }

  _pref(k, v) {
    try {
      if (v === undefined) return localStorage.getItem(`network-devices-card:${k}`);
      localStorage.setItem(`network-devices-card:${k}`, v);
    } catch (e) { /* storage unavailable: preferences are just not remembered */ }
    return null;
  }

  /* ---------- DOM helpers ---------- */
  _node(tag, text, parent, cls) {
    const e = document.createElement(tag);
    if (text !== null && text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    if (parent) parent.append(e);
    return e;
  }
  _icon(name, parent, cls) {
    const i = document.createElement('ha-icon');
    i.setAttribute('icon', ND_MDI.test(name || '') ? name : 'mdi:help-circle-outline');
    if (cls) i.className = cls;
    if (parent) parent.append(i);
    return i;
  }
  _button(label, icon, parent, cls, onClick) {
    const b = this._node('button', null, parent, cls);
    b.type = 'button';
    if (icon) this._icon(icon, b);
    if (label) this._node('span', label, b);
    b.addEventListener('click', ev => { ev.stopPropagation(); onClick(ev); });
    return b;
  }
  _badge(parent, text, icon, cls = '', title = '') {
    const b = this._node('span', null, parent, `badge ${cls}`.trim());
    if (icon) this._icon(icon, b);
    this._node('span', text, b);
    if (title) b.title = title;
    return b;
  }
  // Click-to-copy: the element keeps its text selectable (a drag still selects), a plain click
  // copies `value` (default: its text). Handled by one delegated listener in _build().
  _cp(el, value, what = '') {
    if (value === null || value === undefined || value === '') return el;
    el.classList.add('cp');
    el.dataset.copy = String(value);
    el.tabIndex = 0;
    el.title = [el.title, `Нажмите, чтобы скопировать${what ? ` ${what}` : ''}`].filter(Boolean).join('\n');
    return el;
  }
  _selection() {
    const s = this.shadowRoot.getSelection ? this.shadowRoot.getSelection() : document.getSelection();
    // Chrome: shadowRoot.getSelection() sees ranges inside the card (its toString() is empty there,
    // so check the type); other browsers fall back to the document selection.
    return s && s.type === 'Range' && s.anchorNode ? s : null;
  }
  async _copyText(text) {
    // navigator.clipboard exists only in secure contexts; HA is often opened as http://homeassistant.local.
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* fall through to execCommand */ }
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none';
    const prev = document.activeElement;
    document.body.append(ta);
    ta.select(); ta.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    prev?.focus?.({preventScroll: true});
    return ok;
  }
  async _copy(text) {
    const ok = await this._copyText(text);
    const short = text.length > 60 ? `${text.slice(0, 57)}…` : text;
    this._toast(ok ? `Скопировано: ${short}` : `Не удалось скопировать: ${short}`);
  }
  _onCopyClick(e) {
    const el = e.composedPath().find(n => n instanceof HTMLElement && n.classList.contains('cp'));
    if (!el || (e.type === 'keydown' && e.key !== 'Enter')) return;
    if (e.type === 'click' && this._selection()) return;   // the user is selecting text, not copying
    e.preventDefault(); e.stopPropagation();
    el.classList.add('copied'); setTimeout(() => el.classList.remove('copied'), 900);
    this._copy(el.dataset.copy);
  }
  _svg(tag, attrs, parent) {
    const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  }

  _build() {
    this._built = true;
    this._node('style', ND_CSS, this.shadowRoot);
    this.shadowRoot.addEventListener('click', (e) => this._onCopyClick(e));
    this.shadowRoot.addEventListener('keydown', (e) => this._onCopyClick(e));
    const card = this._node('ha-card', null, this.shadowRoot);
    const head = this._node('div', null, card, 'head');
    const tb = this._node('div', null, head, 'titlebox');
    this._title = this._node('h2', null, tb);
    this._summary = this._node('div', 'Загрузка…', tb, 'muted');
    this._summary.setAttribute('role', 'status');
    const hb = this._node('div', null, head, 'headbtns');
    this._scanBtn = this._button('Сканировать', 'mdi:radar', hb, '', () => this._scan(null));
    this._scanBtn.title = 'Проверить веб-интерфейсы всех онлайн-устройств';
    this._refreshBtn = this._button('Обновить', 'mdi:refresh', hb, '', () => this._poll());
    this._refreshBtn.title = 'Опросить роутер сейчас (обычно раз в час)';
    this._refreshBtn.setAttribute('aria-label', 'Обновить: опросить роутер');
    this._statsBox = this._node('details', null, card, 'stats');
    this._statsBox.hidden = true;
    this._statsBox.open = this._pref('stats') !== '0';
    this._statsBox.addEventListener('toggle', () => this._pref('stats', this._statsBox.open ? '1' : '0'));
    this._chips = this._node('div', null, card, 'chips');
    this._chips.setAttribute('role', 'toolbar');
    this._chips.setAttribute('aria-label', 'Фильтры');
    const tools = this._node('div', null, card, 'tools');
    this._search = this._node('input', null, tools, 'search');
    this._search.type = 'search'; this._search.placeholder = 'Поиск: имя, IP, MAC, вендор…';
    this._search.setAttribute('aria-label', 'Поиск устройств');
    this._search.addEventListener('input', () => { this._query = this._search.value.trim().toLowerCase(); this._shown = ND_PAGE; this._renderList(); });
    this._catSel = this._node('select', null, tools);
    this._catSel.setAttribute('aria-label', 'Категория');
    this._catSel.addEventListener('change', () => { this._cat = this._catSel.value; this._shown = ND_PAGE; this._render(); });
    this._sortSel = this._node('select', null, tools);
    this._sortSel.setAttribute('aria-label', 'Сортировка');
    for (const [k, label] of ND_SORTS) { const o = this._node('option', label, this._sortSel); o.value = k; }
    this._sortSel.addEventListener('change', () => { this._sort = this._sortSel.value; this._renderList(); });
    this._groupBtn = this._button('Группировать по точкам доступа', 'mdi:file-tree', tools, 'chip toggle', () => {
      this._group = !this._group; this._pref('group', this._group ? '1' : '0'); this._render();
    });
    this._err = this._node('div', '', card, 'error');
    this._list = this._node('div', null, card, 'list');
    this._more = this._button('Показать ещё', 'mdi:chevron-down', card, 'more', () => { this._shown += ND_PAGE; this._renderList(); });
    this._foot = this._node('div', '', card, 'foot');
    this._tip = this._node('div', null, this.shadowRoot, 'tip');
    this._tip.setAttribute('role', 'tooltip');
    this._drawer = this._node('div', null, this.shadowRoot, 'drawer-wrap');
    this._drawer.hidden = true;
    this._io = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) { this._io.unobserve(e.target); this._wantHistory(e.target.dataset.mac); }
    }, {rootMargin: '200px'});
    this._render();
  }

  /* ---------- data ---------- */
  // A service counts as available when it is defined and the bridge did not answer not_found
  // in the last 10 minutes (the endpoint may land later: it is retried then).
  _has(service) {
    if (this._config?.mock) return true;
    if (Date.now() - (this._missing.get(service) || 0) < ND_HISTORY_TTL) return false;
    return !!this._hass?.services?.rest_command?.[service];
  }
  _histReady() { return !!this._config?.mock || (!!this._histOk && this._has('network_history')); }
  _notFound(service, e) {
    if (!/not_found|HTTP 404/.test(String(e?.message || e))) return false;
    this._missing.set(service, Date.now());
    return true;
  }

  async _service(service, service_data = {}) {
    if (this._config?.mock && (service === 'network_history' || service === 'network_stats')) return this._mockService(service, service_data);
    const result = await this._hass.callWS({type: 'call_service', domain: 'rest_command', service,
      service_data, return_response: true});
    const response = result?.response;
    const content = response?.content;
    if (!response || response.status !== 200 || !content || content.ok === false) {
      const code = (content && typeof content === 'object' && content.error) || `HTTP ${response?.status ?? '?'}`;
      throw new Error(String(code));
    }
    return content;
  }

  async _load() {
    if (!this._hass) return;
    if (this._busy) { this._reloadWanted = true; return this._inflight; }
    this._inflight = this._loadOnce();
    return this._inflight;
  }

  async _loadOnce() {
    this._busy = true; const seq = ++this._seq;
    try {
      const t0 = performance.now();
      if (!this._perf.firstCall) this._perf.firstCall = Math.round(t0);
      let data = await this._service('network_inventory', {filter: 'all', favicons: false});
      this._perf.inventory = Math.round(performance.now() - t0);
      if (seq !== this._seq) return;
      if (this._config.mock) data = this._mockInventory(data);
      // The server this card runs on is up by definition (its own sweep may miss itself).
      for (const d of data.devices || []) if (d.is_self) d.online = true;
      this._data = data; this._error = ''; this._loadedAt = Date.now();
      this._applyOverrides();
      if (this._drawerMac && !this._selectionIn(this._drawer)) this._renderDrawer();
      this._scheduleScanPoll();
    } catch (e) {
      this._error = `Не удалось получить список устройств: ${this._errText(e)}`;
    } finally {
      this._busy = false;
      const t1 = performance.now();
      this._render();
      this._perf.render = Math.round(performance.now() - t1);
      if (!this._perf.firstPaint && this._data) this._perf.firstPaint = Math.round(performance.now());
    }
    if (Date.now() - this._faviconsAt > ND_FAVICON_TTL) this._idle(() => this._loadFavicons());
    if (this._has('network_stats') && Date.now() - this._statsAt > ND_HISTORY_TTL) this._loadStats();
    // Probe the history endpoint with one device before drawing sparkline placeholders for all rows.
    if (!this._histOk && this._has('network_history')) { const p = this._data?.devices?.find(x => x.online) || this._data?.devices?.[0]; if (p) this._wantHistory(p.mac); }
    // An action finished while a refresh was already in flight: fetch once more so
    // the result (e.g. the "pinned" badge) shows up immediately.
    if (this._reloadWanted) { this._reloadWanted = false; await this._load(); }
  }

  _idle(fn) { (window.requestIdleCallback || ((f) => setTimeout(f, 300)))(fn, {timeout: 3000}); }

  // Favicons travel separately (base64 inside the inventory JSON): once per 30 min, deduplicated
  // into blob: URLs so identical icons are decoded once and the DOM holds short URLs.
  async _loadFavicons() {
    if (this._favLoading) return;
    this._favLoading = true; this._faviconsAt = Date.now();
    try {
      const data = await this._service('network_inventory', {filter: 'all', favicons: true});
      const next = new Map();
      for (const d of data.devices || []) {
        for (const s of d.services || []) {
          const url = s.favicon_data_url;
          if (typeof url !== 'string' || url.length > 200000 || !/^data:image\/(png|x-icon|vnd\.microsoft\.icon|gif|jpeg|svg\+xml|webp);base64,[A-Za-z0-9+/=]+$/i.test(url)) continue;
          let blob = this._faviconBlobs.get(url);
          if (!blob) {
            try { blob = URL.createObjectURL(await (await fetch(url)).blob()); } catch (e) { continue; }
            this._faviconBlobs.set(url, blob);
          }
          next.set(`${d.mac}|${s.port}`, blob);
        }
      }
      this._favicons = next;
      this._renderList();
    } catch (e) { /* favicons are decoration: keep the generic web icons */ }
    finally { this._favLoading = false; }
  }

  async _loadStats() {
    this._statsAt = Date.now();
    try {
      this._stats = await this._service('network_stats', {days: this._config.history_days});
    } catch (e) {
      this._stats = this._notFound('network_stats', e) ? null : {error: this._errText(e)};
    }
    this._renderStats();
  }

  _wantHistory(mac) {
    if (!mac || !this._has('network_history')) return;
    const h = this._history.get(mac);
    if (h && (h.loading || Date.now() - h.at < ND_HISTORY_TTL)) return;
    this._history.set(mac, {...(h || {}), loading: true});
    this._histQueue.push(mac); this._pumpHistory();
  }
  _pumpHistory() {
    while (this._histActive < 2 && this._histQueue.length) {
      const mac = this._histQueue.shift(); this._histActive++;
      this._service('network_history', {device: mac, days: this._config.history_days})
        .then(r => {
          this._history.set(mac, {at: Date.now(), buckets: Array.isArray(r.buckets) ? r.buckets : []});
          if (!this._histOk) { this._histOk = true; this._rows.clear(); this._renderList(); }
        })
        .catch(e => {
          if (this._notFound('network_history', e)) { this._history.delete(mac); this._histQueue = []; this._missingSeen = true; return; }
          this._history.set(mac, {at: Date.now(), buckets: null, error: this._errText(e)});
        })
        .finally(() => {
          this._histActive--;
          // Endpoint not there (yet): drop the sparkline placeholders once, retry in 10 min.
          if (this._missingSeen) { this._missingSeen = false; this._rows.clear(); this._renderList(); if (this._drawerMac) this._renderDrawer(); return; }
          this._paintHistory(mac); this._pumpHistory();
        });
    }
  }

  // "Обновить": the ONLY way this card makes the router be polled (otherwise an hourly timer
  // does it; the auto-refresh above only re-reads the local inventory DB). The bridge
  // serializes polls and reuses one younger than a minute, so repeated clicks are harmless.
  async _poll() {
    if (!this._hass || this._polling) return;
    this._polling = true; this._refreshBtn.disabled = true; this._refreshBtn.classList.add('spin');
    try {
      const r = await this._service('network_update', {});
      this._toast(r.polled ? `Роутер опрошен: ${r.seen ?? '?'} устройств онлайн`
        : r.reason === 'fresh' ? 'Данные уже свежие (опрос меньше минуты назад)' : 'Опрос уже идёт');
      this._hass.callService('homeassistant', 'update_entity',
        {entity_id: ['sensor.network_devices_online', 'sensor.network_inventory_last_poll']}).catch(() => {});
    } catch (e) {
      this._toast(`Не удалось опросить роутер: ${this._errText(e)}`);
    } finally {
      this._polling = false; this._refreshBtn.disabled = false; this._refreshBtn.classList.remove('spin');
    }
    this._statsAt = 0;
    await this._load();
  }

  _scheduleScanPoll() {
    clearTimeout(this._scanTimer);
    if (this._data?.scan?.running) this._scanTimer = setTimeout(() => { this._faviconsAt = 0; this._load(); }, 10000);
  }

  _errText(e) {
    // Bridge errors look like "RuntimeError('router failed (5): router: ... why: ...')": keep the gist.
    const m = String(e?.message || e || 'ошибка').replace(/^\w+Error\(['"]?/, '').replace(/['"]?\)$/, '')
      .replace(/^router failed \(\d+\): (router: )?/, '').split('\\n')[0];
    return ({invalid_mac: 'некорректный MAC', invalid_ip: 'некорректный IP', invalid_icon: 'иконка должна быть вида mdi:имя',
      invalid_name: 'недопустимое имя', name_or_icon_required: 'укажите имя или иконку',
      protected_reservation: 'адрес этого сервера трогать нельзя (он прописан в конфигурации HA)',
      not_reserved: 'у выбранного устройства уже нет резервирования — обновите список', same_device: 'это то же устройство'})[m] || m;
  }

  _toast(message) {
    this.dispatchEvent(new CustomEvent('hass-notification', {detail: {message}, bubbles: true, composed: true}));
  }

  // Optimistic state: `patches` ({mac: {field: value}}) show at once and stay applied on top of
  // re-fetched inventories until the inventory agrees (or 70 min pass: the hourly poll has run).
  _setOverrides(patches) {
    for (const [mac, patch] of Object.entries(patches)) this._overrides.set(mac, {patch, until: Date.now() + 70 * 60e3});
    this._applyOverrides();
    this._render();
    if (this._drawerMac) this._renderDrawer();
  }
  _dropOverrides(macs) { for (const m of macs) this._overrides.delete(m); }
  _applyOverrides() {
    const devices = this._data?.devices || [];
    for (const [mac, o] of [...this._overrides]) {
      const d = devices.find(x => x.mac === mac);
      if (Date.now() > o.until || !d) { if (Date.now() > o.until) this._overrides.delete(mac); continue; }
      const same = Object.entries(o.patch).every(([k, v]) => (d[k] ?? null) === (v ?? null));
      if (same && o.seenFresh) this._overrides.delete(mac);
      else Object.assign(d, o.patch);
    }
  }

  async _act(mac, fn, okText, patches = null) {
    const macs = patches ? Object.keys(patches) : [mac];
    const before = {};
    if (patches) {
      for (const m of macs) {
        const d = (this._data?.devices || []).find(x => x.mac === m);
        if (d) before[m] = Object.fromEntries(Object.keys(patches[m]).map(k => [k, d[k] ?? null]));
      }
    }
    for (const m of macs) this._pending.add(m);
    this._confirm = null; this._editing = null;
    if (patches) this._setOverrides(patches); else this._renderList();
    try {
      const res = await fn();
      const txt = typeof okText === 'function' ? okText(res) : okText;
      // The bridge re-reads the router after a write; if that failed the list catches up at the next poll.
      this._toast(res?.inventory?.refreshed === false ? `${txt}. Список роутера обновится при следующем опросе` : txt);
      if (res?.dry_run) this._dropOverrides(macs);
      // From now on the inventory is authoritative as soon as it agrees with the patch.
      for (const m of macs) { const o = this._overrides.get(m); if (o) o.seenFresh = true; }
    } catch (e) {
      // Roll the optimistic change back.
      this._dropOverrides(macs);
      for (const [m, vals] of Object.entries(before)) {
        const d = (this._data?.devices || []).find(x => x.mac === m);
        if (d) Object.assign(d, vals);
      }
      const slots = /^slots_full:(\d+)$/.exec(String(e?.message || ''));
      if (slots) {
        this._pref('slots', slots[1]);
        this._confirm = {mac, action: 'replace', capacity: Number(slots[1]), choice: null};
        this._toast(`Все ${slots[1]} слотов статических адресов заняты — выберите, какой освободить`);
      } else {
        this._toast(`Ошибка: ${this._errText(e)}`);
      }
    } finally {
      for (const m of macs) this._pending.delete(m);
      this._render();
      if (this._drawerMac) this._renderDrawer();
    }
    await this._load();
  }

  _leaseName(d) {
    // Only a real device name goes to the router's lease table, never a UI placeholder.
    return (d.hostname || (d.names || []).find(Boolean) || '').slice(0, 64);
  }
  _pin(d) {
    const dry = !!this._config.dry_run;
    this._act(d.mac, () => this._service('network_pin', {mac: d.mac, ip: d.ip, name: this._leaseName(d), dry_run: dry}),
      dry ? `Проверка: ${d.ip} был бы закреплён за ${d.mac}` : `${d.ip} закреплён за ${this._name(d)}`,
      dry ? null : {[d.mac]: {reserved_ip: d.ip}});
  }
  _unpin(d) {
    const dry = !!this._config.dry_run;
    this._act(d.mac, () => this._service('network_unpin', {mac: d.mac, dry_run: dry}),
      dry ? `Проверка: резервирование ${d.mac} было бы снято` : `Резервирование для ${this._name(d)} снято`,
      dry ? null : {[d.mac]: {reserved_ip: null}});
  }
  _pinReplace(d, old) {
    const dry = !!this._config.dry_run;
    this._act(d.mac, () => this._service('network_pin_replace',
      {replace_mac: old.mac, mac: d.mac, ip: d.ip, name: this._leaseName(d), dry_run: dry}),
      dry ? `Проверка: ${old.reserved_ip} освободился бы, ${d.ip} закрепился бы за ${d.mac}`
        : `${d.ip} закреплён за ${this._name(d)} вместо ${this._name(old)}`,
      dry ? null : {[old.mac]: {reserved_ip: null}, [d.mac]: {reserved_ip: d.ip}});
  }
  _alias(d, name, icon) {
    const patch = {};
    // router-cli composes display_name from friendly_name (new schema): patch what the alias sets.
    if (name) { if ('friendly_name' in d) patch.friendly_name = name; else patch.display_name = name; }
    if (icon) patch.icon = icon;
    this._act(d.mac, () => this._service('network_alias', {mac: d.mac, name, icon}), 'Сохранено', {[d.mac]: patch});
  }
  // Static-lease capacity: learnt from the router's "all N slots are in use" answer.
  _slotCap() { const n = Number(this._pref('slots')); return Number.isFinite(n) && n > 0 ? n : null; }
  _reserved() { return (this._data?.devices || []).filter(d => d.reserved_ip); }
  _startPin(d) {
    const cap = this._slotCap();
    const full = cap && !d.reserved_ip && this._reserved().length >= cap;
    this._confirm = full ? {mac: d.mac, action: 'replace', capacity: cap, choice: null} : {mac: d.mac, action: 'pin'};
    this._editing = null; this._renderList();
  }
  async _scan(d) {
    if (d) {
      this._act(d.mac, () => this._service('network_scan', {ip: d.ip}),
        r => r.busy ? 'Уже идёт сканирование, попробуйте позже' : r.done ? `${d.ip}: веб-интерфейсы проверены` : `${d.ip}: сканирование продолжается в фоне`);
      this._faviconsAt = 0;
      return;
    }
    this._scanBtn.disabled = true;
    try {
      const r = await this._service('network_scan', {});
      this._toast(r.busy ? 'Сканирование уже идёт' : 'Сканирование запущено — результаты появятся через пару минут');
      await this._load();
    } catch (e) {
      this._toast(`Ошибка сканирования: ${this._errText(e)}`);
    } finally {
      this._scanBtn.disabled = false; this._render();
    }
  }

  /* ---------- helpers ---------- */
  // Rich name: "<product> «<user/device-given name>»", e.g. Google Chromecast HD «Гостиная».
  // Generic: the product comes from router-cli's structured fields (product, brand + model) or its
  // `label`; the personal name from friendly_name / display_name. The product is prefixed only when
  // the personal name doesn't already say what the device is.
  _name(d) {
    // New router-cli schema (brand/model/product/friendly_name): display_name is already the rich
    // "<brand product> «<friendly name>»"; compose it here only while a fresh rename is not in it yet.
    if ('friendly_name' in d || 'brand' in d) {
      const dn = String(d.display_name || ''), fn = String(d.friendly_name || '').trim();
      // router-cli deliberately leaves out serials, SSIDs and generic names, so only a pending
      // (optimistic) rename is composed here.
      if (!fn || dn.includes(fn) || this._overrides.get(d.mac)?.patch?.friendly_name !== fn) return dn || this._plainName(d);
      const product = this._product(d);
      return product && !fn.toLowerCase().includes(product.toLowerCase()) ? `${product} «${fn}»` : fn;
    }
    const own = this._ownName(d);
    const product = this._product(d);
    if (!product) return own || this._plainName(d);
    if (!own) return product;
    const lo = own.toLowerCase(), pl = product.toLowerCase();
    const brand = String(d.brand || product.split(/\s+/)[0] || '').toLowerCase();
    const model = String(d.model || '').toLowerCase();
    if (lo.includes(pl) || pl.includes(lo) || (brand.length >= 3 && lo.includes(brand)) || (model.length >= 3 && lo.includes(model))) return own;
    return `${product} «${own}»`;
  }
  _plainName(d) {
    return d.display_name || d.hostname || (d.names || []).find(Boolean) || d.vendor || (this._randomMac(d) ? 'Устройство с приватным MAC' : 'Неизвестное устройство');
  }
  _product(d) {
    const brand = String(d.brand || '').trim(), model = String(d.model || '').trim();
    const withBrand = (s) => (brand && s && !s.toLowerCase().includes(brand.toLowerCase()) ? `${brand} ${s}` : s);
    if (d.product) return withBrand(String(d.product).trim());
    if (model) return withBrand(model);
    if (d.label) return withBrand(String(d.label).trim());
    return brand || '';
  }
  // A name a person (or the device's owner) gave it — not a model string, generic word or serial.
  _ownName(d) {
    const cand = String(d.friendly_name || d.display_name || '').trim();
    if (!cand) return '';
    if (/^(tv|television|телевизор|phone|iphone|ipad|android|laptop|desktop|computer|pc|printer|speaker|camera|router|device|unknown|localhost|esp32?[-\w]*)$/i.test(cand)) return '';
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(cand) || /\d{6,}/.test(cand) || /^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$/i.test(cand)) return '';
    if (d.vendor && cand.toLowerCase() === String(d.vendor).toLowerCase()) return '';
    if (/^(device with a private mac|устройство с приватным mac)$/i.test(cand)) return '';
    return cand;
  }
  // Where a piece of network gear stands (router-cli: location / placement / room).
  _loc(d) { return String(d?.location || d?.placement || d?.room || '').trim(); }
  // Short access-point label for topology: "Xiaomi AX3000 · Гостиная".
  _apLabel(ap) {
    const short = this._product(ap) || this._name(ap);
    return [short, this._loc(ap)].filter(Boolean).join(' · ');
  }
  _viaLabel(c) {
    if (!c) return '';
    const ap = c.via ? (this._data?.devices || []).find(x => x.mac === c.via) : null;
    const loc = c.via_location || (ap && this._loc(ap)) || '';
    const base = ap ? (this._product(ap) || this._name(ap)) : (c.via_name || '');
    return [base, loc].filter(Boolean).join(' · ');
  }
  _selectionIn(el) {
    const s = this._selection();
    return !!(s && el && s.anchorNode && el.contains(s.anchorNode));
  }
  _randomMac(d) {
    if (d.random_mac) return true;
    const first = parseInt(String(d.mac || '').slice(0, 2), 16);
    return Number.isFinite(first) && (first & 0x02) === 0x02;   // locally administered bit
  }
  _pinnable(d) { return d.pinnable === true || d.pinnable === false ? d.pinnable : !this._randomMac(d); }
  _conn(d) { return d.connection && typeof d.connection === 'object' ? d.connection : null; }
  _connType(d) {
    const c = this._conn(d);
    if (c?.type === 'wifi' || c?.type === 'wired') return c.type;
    return null;
  }
  _isGear(d) { return !!d.is_network_gear; }
  _iconFor(d) {
    if (d.category && ND_MDI.test(d.icon || '')) return d.icon;   // router-cli classified it (or the user's override)
    if (ND_MDI.test(d.icon || '') && d.icon !== 'mdi:lan-connect' && d.icon !== 'mdi:help-network') return d.icon;
    if (d.category && this._catInfo(d.category)) return this._catInfo(d.category)[1];
    const hay = [d.hostname, ...(d.names || []), d.vendor, ...(d.services || []).map(s => s.title)].filter(Boolean).join(' ');
    for (const [re, icon] of ND_ICON_RULES) if (re.test(hay)) return icon;
    if (ND_MDI.test(d.icon || '')) return d.icon;
    return this._randomMac(d) ? 'mdi:incognito' : 'mdi:lan-connect';
  }
  _catInfo(c) { return ND_CATS[String(c).toLowerCase().replace(/-/g, '_')] || null; }
  _catLabel(c) { return this._catInfo(c)?.[0] || String(c).replace(/[_-]/g, ' '); }
  _guessed(c) { return c?.source === 'heuristic'; }   // router-cli inferred the link type from the device type
  _conf(d) {
    const c = Number(d.confidence);
    if (!Number.isFinite(c) || c <= 0) return null;
    return Math.round(c <= 1 ? c * 100 : Math.min(c, 100));
  }
  _band(b) {
    if (b === null || b === undefined || b === '') return '';
    const s = String(b).trim();
    if (/^\d+(\.\d+)?$/.test(s)) return `${s.replace('.', ',')} ГГц`;
    return s.replace(/\s*ghz/i, ' ГГц').replace('2.4', '2,4');
  }
  _connText(d) {
    const c = this._conn(d);
    if (!c) return null;
    const maybe = this._guessed(c) ? 'вероятно ' : '';
    if (c.type === 'wired') return ['mdi:ethernet', [`${maybe}кабель`, this._viaLabel(c)].filter(Boolean).join(' · ')];
    if (c.type === 'wifi') {
      const rssi = this._num(c.rssi) ?? NaN;
      return [this._wifiIcon(rssi), [`${maybe}Wi‑Fi`, this._viaLabel(c), this._band(c.band), Number.isFinite(rssi) && c.rssi !== null ? `−${Math.abs(Math.round(rssi))} дБм` : '']
        .filter(Boolean).join(' · ')];
    }
    return null;
  }
  _wifiIcon(rssi) {
    if (!Number.isFinite(rssi)) return 'mdi:wifi';
    const r = -Math.abs(rssi);
    return r >= -55 ? 'mdi:wifi-strength-4' : r >= -67 ? 'mdi:wifi-strength-3' : r >= -75 ? 'mdi:wifi-strength-2' : 'mdi:wifi-strength-1';
  }
  _num(v) { if (v === null || v === undefined || v === "") return null; const n = Number(v); return Number.isFinite(n) ? n : null; }
  _ts(v) { const t = v ? Date.parse(v) : NaN; return Number.isFinite(t) ? t : 0; }
  _tms(t) { if (typeof t === 'number') return t < 1e12 ? t * 1000 : t; return this._ts(t); }
  _ago(v) {
    const t = this._ts(v); if (!t) return '—';
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return 'только что';
    if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
    if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
    if (s < 86400 * 60) return `${Math.floor(s / 86400)} дн назад`;
    return new Date(t).toLocaleDateString('ru-RU');
  }
  _bytes(n) {
    n = Number(n); if (!Number.isFinite(n) || n < 0) return '—';
    const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1).replace('.', ',')} ${u[i]}`;
  }
  _rate(n) {   // bytes/s → bits/s
    n = Number(n) * 8; if (!Number.isFinite(n) || n <= 0) return '';
    const u = ['бит/с', 'Кбит/с', 'Мбит/с', 'Гбит/с']; let i = 0;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return `${n >= 100 ? Math.round(n) : n.toFixed(1).replace('.', ',')} ${u[i]}`;
  }
  _traffic(d) {
    const t = d.traffic && typeof d.traffic === 'object' ? d.traffic : null;
    if (!t) return null;
    const rx = Number(t.rx_bytes), tx = Number(t.tx_bytes);
    if (!Number.isFinite(rx) && !Number.isFinite(tx)) return null;
    return {rx: Number.isFinite(rx) ? rx : 0, tx: Number.isFinite(tx) ? tx : 0, rxr: Number(t.rx_rate) || 0, txr: Number(t.tx_rate) || 0, t};
  }
  _ipKey(ip) { return String(ip || '').split('.').reduce((a, o) => a * 256 + (parseInt(o, 10) || 0), 0); }
  _isNew(d) { return Date.now() - this._ts(d.first_seen) <= this._config.new_hours * 3600e3; }
  _matches(d, f) {
    switch (f) {
      case 'recent': return d.online || Date.now() - this._ts(d.last_seen) <= this._config.recent_hours * 3600e3;
      case 'active': return !!d.online;
      case 'pinned': return !!d.reserved_ip;
      case 'new': return this._isNew(d);
      case 'unknown': return !d.vendor;
      case 'wifi': return this._connType(d) === 'wifi';
      case 'wired': return this._connType(d) === 'wired';
      case 'gear': return this._isGear(d);
      default: return true;
    }
  }
  _serviceUrl(s, d) {
    try {
      const u = new URL(s.url || `${s.scheme || 'http'}://${d.ip}:${s.port}/`);
      return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
    } catch (e) { return null; }
  }
  // → 'ok' | 'error' (answered with 4xx/5xx or a TLS problem) | 'down' (timeout/refused) | 'unknown'
  _svcHealth(s) {
    const code = Number(s.http_status);
    if (s.reachable === false) return 'down';
    if (Number.isFinite(code) && code >= 400) return 'error';
    if (s.reachable === true && s.error) return 'error';
    if (s.reachable === true) return 'ok';
    return 'unknown';
  }

  /* ---------- rendering ---------- */
  _render() {
    if (!this._built || !this._config) return;
    this._title.textContent = this._config.title || 'Устройства в сети';
    if (this._config.dry_run) this._node('span', 'тестовый режим', this._title, 'test').title = 'Закрепление/снятие IP вызывает router-cli с --dry-run';
    if (this._config.mock) this._node('span', 'мок-данные', this._title, 'test').title = 'Категории, подключение, трафик и история сгенерированы в браузере';
    const devices = this._data?.devices || [];
    const online = devices.filter(d => d.online).length;
    const parts = [];
    if (this._data) {
      parts.push(`${devices.length} устройств, ${online} онлайн`);
      const r = this._data.router || {};
      if (r.model || r.host) parts.push(`роутер ${[r.model, r.host].filter(Boolean).join(' · ')}`);
      // last_poll = when the router was last asked (router-cli); generated_at = when the
      // local DB was read (older router-cli without last_poll).
      const polled = this._data.last_poll || this._data.generated_at;
      if (polled) parts.push(`обновлено ${this._ago(polled)}`);
      if (this._data.scan?.running) parts.push(`идёт сканирование: ${this._data.scan.running}`);
    } else if (!this._error) parts.push('Загрузка…');
    this._summary.textContent = parts.join(' · ');
    this._scanBtn.classList.toggle('spin', !!this._data?.scan?.running);
    this._err.textContent = this._error;

    // Chips: connection/gear chips appear only once router-cli reports that data.
    const hasConn = devices.some(d => this._connType(d));
    const hasGear = devices.some(d => this._isGear(d));
    const chipSig = JSON.stringify([this._filter, hasConn, hasGear, ND_FILTERS.map(([k]) => devices.filter(d => this._matches(d, k)).length)]);
    if (chipSig !== this._chipSig) {
      this._chipSig = chipSig;
      this._chips.replaceChildren();
      for (const [key, label, icon] of ND_FILTERS) {
        if ((key === 'wifi' || key === 'wired') && !hasConn && this._filter !== key) continue;
        if (key === 'gear' && !hasGear && this._filter !== key) continue;
        const b = this._button(null, icon || null, this._chips, 'chip', () => { this._filter = key; this._shown = ND_PAGE; this._render(); });
        this._node('span', label, b);
        this._node('span', String(devices.filter(d => this._matches(d, key)).length), b, 'n');
        b.setAttribute('aria-pressed', String(this._filter === key));
      }
    }
    // Category dropdown from the categories present.
    const cats = new Map();
    for (const d of devices) if (d.category) cats.set(d.category, (cats.get(d.category) || 0) + 1);
    const catSig = JSON.stringify([...cats]);
    if (catSig !== this._catSig) {
      this._catSig = catSig;
      this._catSel.replaceChildren();
      this._node('option', 'Все категории', this._catSel).value = '';
      for (const [c, n] of [...cats].sort((a, b) => this._catLabel(a[0]).localeCompare(this._catLabel(b[0]), 'ru'))) {
        this._node('option', `${this._catLabel(c)} (${n})`, this._catSel).value = c;
      }
      if (this._cat && !cats.has(this._cat)) this._cat = '';
    }
    this._catSel.hidden = !cats.size;
    this._catSel.value = this._cat;
    this._groupBtn.hidden = !hasConn && !hasGear;
    this._groupOn = this._group && (hasConn || hasGear);
    this._groupBtn.setAttribute('aria-pressed', String(this._group));
    this._sortSel.value = this._sort;
    this._renderStats();
    this._renderList();
  }

  _filtered() {
    const q = this._query;
    let items = (this._data?.devices || []).filter(d => this._matches(d, this._filter) && (!this._cat || d.category === this._cat));
    if (q) items = items.filter(d => [this._name(d), d.hostname, ...(d.names || []), d.ip, d.mac, d.vendor, d.reserved_ip,
      d.category && this._catLabel(d.category), this._viaLabel(this._conn(d)), d.label, this._loc(d), ...(d.interfaces || []).flatMap(i => [i.mac, i.ip]),
      ...(d.services || []).map(s => s.title)].filter(Boolean).join(' ').toLowerCase().includes(q));
    const byName = (a, b) => this._name(a).localeCompare(this._name(b), 'ru');
    const tr = (d) => { const t = this._traffic(d); return t ? t.rx + t.tx : -1; };
    const cmp = {
      last_seen: (a, b) => (b.online - a.online) || (this._ts(b.last_seen) - this._ts(a.last_seen)) || byName(a, b),
      name: byName,
      ip: (a, b) => this._ipKey(a.ip) - this._ipKey(b.ip),
      traffic: (a, b) => (tr(b) - tr(a)) || byName(a, b),
    }[this._sort] || byName;
    return items.sort(cmp);
  }

  // → [{key, header?, device?, child?}] in display order.
  _layout(items) {
    if (!this._groupOn) return items.map(d => ({key: d.mac, device: d}));
    const all = this._data?.devices || [];
    const byMac = new Map(all.map(d => [d.mac, d]));
    const groups = new Map();   // groupKey → {gear, clients[]}
    const put = (k, d) => { if (!groups.has(k)) groups.set(k, {clients: []}); groups.get(k).clients.push(d); };
    for (const d of items) {
      const c = this._conn(d);
      if (this._isGear(d)) { if (!groups.has(d.mac)) groups.set(d.mac, {clients: []}); groups.get(d.mac).gear = d; continue; }
      if (c?.via) put(c.via, d);
      else put(c?.type === 'wired' ? '~wired' : c?.type === 'wifi' ? '~wifi' : '~other', d);
    }
    // Gear without known clients is listed together; the rest are pseudo-groups by link type.
    const lone = [];
    for (const [k, g] of groups) if (g.gear && !g.clients.length) { lone.push(g.gear); groups.delete(k); }
    const out = [];
    const keys = [...groups.keys()].sort((a, b) => {
      const rank = (k) => ({'~wifi': 3, '~wired': 4, '~other': 5})[k] || 1;
      const na = byMac.get(a) ? this._name(byMac.get(a)) : a, nb = byMac.get(b) ? this._name(byMac.get(b)) : b;
      return rank(a) - rank(b) || na.localeCompare(nb, 'ru');
    });
    const header = (key, label, icon, n, gear = false) => out.push({key: `g:${key}`, header: {label, icon, n, gear}});
    for (const k of keys) {
      const g = groups.get(k);
      if (g.gear) {
        // The highlighted gear row is the group header; its clients are indented below it.
        out.push({key: g.gear.mac, device: g.gear, clients: g.clients.length});
      } else if (k.startsWith('~')) {
        header(k, k === '~wifi' ? 'Wi‑Fi (точка доступа не определена)' : k === '~wired' ? 'Кабель (точка не определена)' : 'Подключение неизвестно',
          k === '~wifi' ? 'mdi:wifi' : k === '~wired' ? 'mdi:ethernet' : 'mdi:help-network', g.clients.length);
      } else {
        // Clients of an access point that is filtered out of the list.
        const ap = byMac.get(k);
        header(k, ap ? this._apLabel(ap) : g.clients.map(d => this._viaLabel(this._conn(d))).find(Boolean) || k,
          ap ? this._iconFor(ap) : 'mdi:access-point', g.clients.length, true);
      }
      for (const d of g.clients) out.push({key: d.mac, device: d, child: true});
    }
    if (lone.length) {
      const at = out.findIndex(it => it.key.startsWith('g:~'));
      const block = [{key: 'g:~gear', header: {label: 'Сетевое оборудование', icon: 'mdi:router-network', n: lone.length}},
        ...lone.sort((a, b) => this._name(a).localeCompare(this._name(b), 'ru')).map(d => ({key: d.mac, device: d}))];
      out.splice(at < 0 ? out.length : at, 0, ...block);
    }
    return out;
  }

  _renderList() {
    if (!this._built) return;
    const items = this._filtered();
    const layout = this._layout(items);
    const visible = layout.slice(0, this._shown);
    const admin = !!this._hass?.user?.is_admin;
    const keep = new Set();
    const els = [];
    for (const it of visible) {
      keep.add(it.key);
      let sig, make;
      if (it.header) {
        sig = JSON.stringify(it.header);
        make = () => this._groupHeader(it.header);
      } else {
        const d = it.device;
        sig = JSON.stringify([d, !!it.child, it.clients ?? null, admin, this._pending.has(d.mac), this._confirm?.mac === d.mac ? [this._confirm, this._confirm.action === 'replace' ? this._reserved().map(x => [x.mac, x.reserved_ip, x.online, this._name(x)]) : 0] : '',
          this._editing === d.mac, this._open.has(d.mac), this._histReady(), this._ago(d.last_seen), this._ago(d.first_seen),
          (d.services || []).map(s => this._favicons.get(`${d.mac}|${s.port}`) || '')]);
        make = () => this._row(d, it.child, it.clients);
      }
      const prev = this._rows.get(it.key);
      // Never rebuild a row with an open editor: that would wipe a half-typed name.
      // ...nor one the user is selecting text in (the periodic refresh would drop the selection).
      if (prev && (prev.sig === sig || (this._editing === it.key && prev.editing) || this._selectionIn(prev.el))) { els.push(prev.el); continue; }
      let el;
      try { el = make(); } catch (e) {
        // One device with unexpected data must not blank the whole list.
        console.error('network-devices-card: row render failed', it.key, e);
        el = this._node('div', `${it.device ? `${it.device.ip || ''} ${it.device.mac}` : it.key}: не удалось отрисовать`, null, 'row muted');
      }
      this._rows.set(it.key, {sig, el, editing: this._editing === it.key});
      els.push(el);
    }
    for (const k of [...this._rows.keys()]) if (!keep.has(k)) this._rows.delete(k);
    const cur = [...this._list.children];
    if (cur.length !== els.length || cur.some((e, i) => e !== els[i])) this._list.replaceChildren(...els);
    if (!items.length && this._data) this._list.replaceChildren(this._node('div', this._query ? 'Ничего не найдено' : 'Нет устройств для этого фильтра', null, 'empty'));
    this._more.hidden = layout.length <= this._shown;
    const total = this._data?.devices?.length || 0;
    this._foot.textContent = this._data ? `Показано ${items.length} из ${total}. Роутер опрашивается раз в час или кнопкой «Обновить»; список перечитывается каждые ${Math.max(15, Number(this._config.refresh_interval) || 60)} с.` : '';
  }

  _groupHeader(h) {
    const el = this._node('div', null, null, `group${h.gear ? ' gear' : ''}`);
    this._icon(h.icon, el);
    this._node('span', h.label, el, 'gl');
    this._node('span', h.gear ? `${h.n} ${this._plural(h.n, 'клиент', 'клиента', 'клиентов')}` : String(h.n), el, 'n');
    return el;
  }
  _plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
  }

  _row(d, child, clients) {
    const admin = !!this._hass?.user?.is_admin;
    const busy = this._pending.has(d.mac);
    const gear = this._isGear(d);
    const row = this._node('div', null, null, `row${d.online ? '' : ' offline'}${gear ? ' gear' : ''}${child ? ' child' : ''}`);
    // The avatar opens the details drawer; the name itself is click-to-copy.
    const av = this._node('button', null, row, 'avatar');
    av.type = 'button'; av.title = 'Подробнее: активность, трафик, интерфейсы';
    av.setAttribute('aria-label', `Подробнее: ${this._name(d)}`);
    av.addEventListener('click', (e) => { e.stopPropagation(); this._openDrawer(d.mac); });
    this._icon(this._iconFor(d), av);
    const dot = this._node('span', null, av, `dot${d.online ? ' on' : ''}`);
    dot.title = d.online ? 'онлайн' : 'офлайн';
    const main = this._node('div', null, row, 'main');

    const nm = this._node('div', null, main, 'name');
    const name = this._name(d);
    this._cp(this._node('span', name, nm, 'nm'), name, 'имя');
    if (gear && this._loc(d)) this._badge(nm, this._loc(d), 'mdi:map-marker', 'gearb', 'Где стоит');
    if (d.is_self) this._badge(nm, 'этот сервер', 'mdi:home-assistant', 'gearb', 'Компьютер, на котором работает Home Assistant');
    if (gear) this._badge(nm, 'сетевое оборудование', 'mdi:router-wireless', 'gearb', 'Роутер, mesh‑узел или точка доступа');
    if (clients) this._badge(nm, `${clients} ${this._plural(clients, 'клиент', 'клиента', 'клиентов')}`, 'mdi:devices', '', 'Устройства, подключённые через эту точку доступа (показаны ниже)');
    if (this._isNew(d)) this._badge(nm, 'новое', null, 'new');
    if (d.category) {
      const conf = this._conf(d);
      this._badge(nm, `${this._catLabel(d.category)}${conf !== null ? ` · ${conf}%` : ''}`, null, 'cat',
        `Категория определена автоматически${conf !== null ? `, уверенность ${conf}%` : ''}${d.label ? `\nОпознано как: ${d.label}` : ''}`);
    }
    if (this._randomMac(d)) {
      this._badge(nm, 'приватный MAC', 'mdi:incognito', '',
        'Телефон/часы/ноутбук с приватным (случайным) MAC: адрес меняется по сети и со временем, поэтому IP закреплять бесполезно');
    }

    const l1 = this._node('div', null, main, 'line');
    if (d.ip) this._cp(this._node('span', d.ip, l1, 'mono ip'), d.ip, 'IP');
    else this._node('span', '—', l1, 'mono ip');
    if (d.reserved_ip && d.reserved_ip === d.ip) this._badge(l1, 'закреплён', 'mdi:pin', 'pin');
    else if (d.reserved_ip) this._cp(this._badge(l1, `закреплён ${d.reserved_ip}${d.ip ? ', сейчас другой IP' : ''}`, 'mdi:alert', 'warn'), d.reserved_ip, 'закреплённый IP');
    const ct = this._connText(d);
    if (ct) {
      const c = this._conn(d);
      const guess = this._guessed(c);
      this._badge(l1, ct[1], ct[0], `conn${c.type === 'wifi' ? '' : ' wired'}${guess ? ' guess' : ''}`,
        guess ? `Тип подключения предположен по типу устройства (роутер не сообщает, кто подключён по Wi‑Fi, а кто кабелем)`
          : c.type === 'wifi' ? `Wi‑Fi через ${this._viaLabel(c) || c.via || 'точку доступа'}${this._num(c.rssi) !== null ? `, уровень сигнала ${c.rssi} дБм` : ''}` : 'Подключено кабелем');
    }
    // router-cli gives [{ip, first_seen, last_seen}]; accept plain strings too.
    const prev = (d.ip_history || []).map(h => typeof h === 'string' ? h : h?.ip).filter(ip => ip && ip !== d.ip);
    if (prev.length) this._node('span', `ранее: ${[...new Set(prev)].slice(-3).join(', ')}`, l1).title = 'История IP';

    const l2 = this._node('div', null, main, 'line');
    this._cp(this._node('span', d.mac, l2, 'mono'), d.mac, 'MAC');
    if (d.vendor) this._cp(this._node('span', d.vendor, l2), d.vendor, 'производителя');
    if (d.hostname && d.hostname !== name && !name.includes(d.hostname)) {
      const h = this._cp(this._node('span', d.hostname, l2, 'mono host'), d.hostname, 'имя хоста');
      h.title = `Имя хоста (DHCP/mDNS)\n${h.title}`;
    }
    if (d.same_device_as) {
      const other = (this._data?.devices || []).find(x => x.mac === d.same_device_as);
      this._badge(l2, `тот же, что ${other ? this._name(other) : d.same_device_as}`, 'mdi:link-variant', '',
        `Это другой сетевой интерфейс того же устройства (${d.same_device_as}${other?.ip ? `, ${other.ip}` : ''})`);
    }
    const ifs = this._ifaces(d);
    if (!ct && d.interface && ifs.length < 2) this._node('span', d.interface === 'lan' ? 'LAN' : d.interface, l2).title = 'Интерфейс по данным роутера';
    if (ifs.length > 1) {
      const box = this._node('div', null, main, 'ifs');
      box.setAttribute('aria-label', 'Сетевые интерфейсы устройства');
      for (const i of ifs) this._ifaceChip(box, i);
    }

    const l3 = this._node('div', null, main, 'line');
    this._node('span', d.is_self ? 'онлайн (этот сервер)' : d.online ? 'онлайн' : `был ${this._ago(d.last_seen)}`, l3).title = d.last_seen ? new Date(this._ts(d.last_seen)).toLocaleString('ru-RU') : '';
    this._node('span', `впервые ${this._ago(d.first_seen)}`, l3).title = d.first_seen ? new Date(this._ts(d.first_seen)).toLocaleString('ru-RU') : '';
    const tr = this._traffic(d);
    if (tr) {
      const t = this._node('span', null, l3, 'traffic');
      t.title = `Получено ${this._bytes(tr.rx)}, отправлено ${this._bytes(tr.tx)}${tr.t.updated_at ? ` (данные ${this._ago(tr.t.updated_at)})` : ''}${tr.t.source ? `, источник: ${tr.t.source}` : ''}`;
      this._icon('mdi:arrow-down', t); this._node('span', this._bytes(tr.rx), t);
      this._icon('mdi:arrow-up', t); this._node('span', this._bytes(tr.tx), t);
      const rate = this._rate(tr.rxr + tr.txr);
      if (rate && d.online) this._node('span', `· ${rate}`, t, 'rate');
    }
    if (this._histReady()) {
      const sp = this._node('span', null, l3, 'spark');
      sp.dataset.mac = d.mac;
      sp.setAttribute('aria-label', `Онлайн по часам за ${this._config.history_days} дней`);
      this._paintSpark(sp, d.mac);
      const h = this._history.get(d.mac);
      if (!h || (!h.loading && Date.now() - h.at > ND_HISTORY_TTL)) this._io.observe(sp);
    }

    const svcs = (d.services || []).map(s => [s, this._serviceUrl(s, d)]).filter(([, u]) => u);
    if (svcs.length) {
      const box = this._node('div', null, main, 'svcs');
      for (const [s, url] of svcs.sort((a, b) => (a[0].port || 0) - (b[0].port || 0))) this._svcChip(box, d, s, url);
    }

    if (d.category || (Array.isArray(d.evidence) && d.evidence.length) || this._randomMac(d)) this._why(main, d);

    const acts = this._node('div', null, row, 'acts');
    if (admin) {
      const pinned = d.reserved_ip && d.reserved_ip === d.ip;
      if (!pinned && d.ip && this._pinnable(d)) {
        const b = this._button(d.reserved_ip ? 'Перезакрепить' : 'Закрепить IP', 'mdi:pin-outline', acts, busy ? 'spin' : '', () => this._startPin(d));
        b.title = `Зарезервировать ${d.ip} за ${d.mac} в DHCP роутера`; b.disabled = busy;
      }
      if (d.reserved_ip) {
        const b = this._button('', 'mdi:pin-off-outline', acts, 'icon',
          () => { this._confirm = {mac: d.mac, action: 'unpin'}; this._editing = null; this._renderList(); });
        b.title = 'Снять резервирование'; b.setAttribute('aria-label', 'Снять резервирование'); b.disabled = busy || !!d.is_self;
        if (d.is_self) b.title = 'Адрес этого сервера прописан в конфигурации HA — не снимать';
      }
      const sc = this._button('', 'mdi:web-refresh', acts, 'icon', () => this._scan(d));
      sc.title = 'Проверить веб-интерфейсы'; sc.setAttribute('aria-label', 'Проверить веб-интерфейсы'); sc.disabled = busy || !d.ip;
      const ed = this._button('', 'mdi:pencil', acts, 'icon',
        () => { this._editing = this._editing === d.mac ? null : d.mac; this._confirm = null; this._renderList(); });
      ed.title = 'Имя и иконка'; ed.setAttribute('aria-label', 'Имя и иконка'); ed.disabled = busy;
    }
    const info = this._button('', 'mdi:chart-box-outline', acts, 'icon', () => this._openDrawer(d.mac));
    info.title = 'Активность и трафик'; info.setAttribute('aria-label', 'Активность и трафик');

    if (admin && this._confirm?.mac === d.mac) this._confirmPanel(d, row, busy);
    if (admin && this._editing === d.mac) this._editPanel(d, row, busy);
    return row;
  }

  // All network interfaces of a device (router-cli folds multi-MAC devices into one entry).
  _ifaces(d) {
    const list = (Array.isArray(d.interfaces) ? d.interfaces : []).filter(i => i && i.mac);
    return list.length ? list : [{mac: d.mac, ip: d.ip, online: d.online, name: null, type: this._connType(d) || 'unknown'}];
  }
  _ifaceChip(box, i) {
    const chip = this._node('span', null, box, `iface${i.online ? '' : ' off'}`);
    const icon = i.type === 'wifi' ? 'mdi:wifi' : i.type === 'wired' ? 'mdi:ethernet' : 'mdi:lan';
    this._icon(icon, chip).title = i.type === 'wifi' ? 'Wi‑Fi' : i.type === 'wired' ? 'кабель' : 'тип неизвестен';
    if (i.name) this._node('span', i.name, chip, 'in');
    this._cp(this._node('span', i.mac, chip, 'mono'), i.mac, 'MAC');
    if (i.ip) this._cp(this._node('span', i.ip, chip, 'mono'), i.ip, 'IP');
    chip.title = `${i.online ? 'онлайн' : 'офлайн'}`;
    return chip;
  }

  _svcChip(box, d, s, url) {
    const health = this._svcHealth(s);
    const wrap = this._node('span', null, box, 'svcw');
    const a = this._node('a', null, wrap, `svc ${health}`);
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    const code = Number(s.http_status);
    const state = health === 'down' ? `Недоступен${s.error ? `: ${s.error}` : ''}`
      : health === 'error' ? `Отвечает с ошибкой${Number.isFinite(code) && code ? ` HTTP ${code}` : ''}${s.error ? `: ${s.error}` : ''}`
      : health === 'ok' ? `Доступен${Number.isFinite(code) && code ? ` (HTTP ${code})` : ''}` : '';
    a.title = [s.title, s.server, url, state, s.checked_at ? `проверено ${this._ago(s.checked_at)}` : ''].filter(Boolean).join('\n');
    const fav = this._favicons.get(`${d.mac}|${s.port}`);
    if (health === 'down') this._icon('mdi:web-off', a, 'st');
    else if (health === 'error') this._icon('mdi:alert-circle-outline', a, 'st');
    else if (fav) { const img = this._node('img', null, a); img.src = fav; img.alt = ''; img.loading = 'lazy'; img.decoding = 'async'; img.width = 16; img.height = 16; }
    else this._icon(s.scheme === 'https' ? 'mdi:web-check' : 'mdi:web', a);
    this._node('span', (s.title || '').trim() || s.server || (s.scheme || 'http').toUpperCase(), a, 't');
    this._node('span', `:${s.port}`, a, 'p');
    const cb = this._node('button', null, wrap, 'svccp cp');
    cb.type = 'button'; cb.dataset.copy = url;
    cb.title = `Скопировать адрес: ${url}`; cb.setAttribute('aria-label', `Скопировать ${url}`);
    this._icon('mdi:content-copy', cb);
  }

  _why(main, d) {
    const det = this._node('details', null, main, 'why');
    det.open = this._open.has(d.mac);
    det.addEventListener('toggle', () => { if (det.open) this._open.add(d.mac); else this._open.delete(d.mac); });
    const sum = this._node('summary', null, det);
    this._icon('mdi:chevron-right', sum, 'chev');
    this._node('span', d.category ? `Почему «${this._catLabel(d.category)}»?` : 'Подробности', sum);
    const body = this._node('div', null, det, 'whybody');
    if (d.category) {
      const conf = this._conf(d);
      const p = this._node('div', null, body);
      p.textContent = `Категория: ${this._catLabel(d.category)}${conf !== null ? `, уверенность ${conf}%` : ''}. Имя: ${this._name(d)}.`;
    }
    const ev = (Array.isArray(d.evidence) ? d.evidence : []).filter(e => e && typeof e === 'object')
      .sort((a, b) => (Number(b.weight) || 0) - (Number(a.weight) || 0));
    if (ev.length) {
      const ul = this._node('ul', null, body, 'ev');
      for (const e of ev) {
        const li = this._node('li', null, ul);
        this._node('span', String(e.source || '—'), li, 'src');
        this._node('span', String(e.detail || ''), li, 'det');
        const w = Number(e.weight);
        if (Number.isFinite(w)) {
          const wb = this._node('span', null, li, 'w');
          wb.title = `вес ${w}`;
          this._node('span', null, wb, 'wf').style.width = `${Math.max(4, Math.min(100, (w <= 1 ? w * 100 : w)))}%`;
        }
      }
    } else if (d.category) this._node('div', 'Причина определения категории неизвестна.', body, 'muted');
    const alts = (Array.isArray(d.alternatives) ? d.alternatives : []).filter(a => a && a.category);
    if (alts.length) {
      this._node('div', `Другие варианты: ${alts.map(a => `${this._catLabel(a.category)}${this._conf(a) !== null ? ` ${this._conf(a)}%` : ''}`).join(', ')}.`, body, 'muted');
    }
    if (this._guessed(this._conn(d))) {
      this._node('div', 'Wi‑Fi/кабель здесь — предположение по типу устройства: роутер этого не сообщает.', body, 'muted');
    }
    if (this._randomMac(d)) {
      this._node('div', 'Приватный (случайный) MAC: так делают телефоны, часы и ноутбуки. Адрес меняется при переподключении, поэтому закреплять IP бесполезно — кнопка «Закрепить IP» скрыта.', body, 'muted');
    }
  }

  _confirmPanel(d, row, busy) {
    if (this._confirm.action === 'replace') { this._replacePanel(d, row, busy); return; }
    const p = this._node('div', null, row, 'panel');
    const pin = this._confirm.action === 'pin';
    const dry = this._config.dry_run ? ' (проверка)' : '';
    this._node('span', pin
      ? `Закрепить ${d.ip} за ${d.mac} (${this._name(d)})${d.reserved_ip ? `, заменив ${d.reserved_ip}` : ''}?${dry}`
      : `Снять резервирование ${d.reserved_ip} для ${d.mac}?${dry}`, p);
    const yes = this._button(pin ? 'Закрепить' : 'Снять', pin ? 'mdi:pin' : 'mdi:pin-off', p, pin ? 'primary' : 'danger',
      () => pin ? this._pin(d) : this._unpin(d));
    yes.disabled = busy;
    this._button('Отмена', null, p, '', () => { this._confirm = null; this._renderList(); });
    if (!busy) requestAnimationFrame(() => yes.focus());
  }

  // Why a reservation is a good one to give up (higher score = better candidate).
  _replaceScore(r, all) {
    const reasons = [];
    let score = 0;
    if (r.is_self) return {score: -1, reasons: ['этот сервер — не трогать'], blocked: true};
    const offH = r.online ? 0 : Math.max(0, (Date.now() - this._ts(r.last_seen)) / 3600e3);
    if (!r.online) { score += 10 + Math.min(offH, 24 * 30); reasons.push(r.last_seen ? `офлайн ${this._ago(r.last_seen).replace(' назад', '')}` : 'офлайн, когда — неизвестно'); }
    if (!r.ip) { score += 20; reasons.push('нет текущего IP'); }
    else if (r.ip !== r.reserved_ip) { score += 30; reasons.push(`сейчас ${r.ip} — резерв не используется`); }
    if (!r.category || r.category === 'unknown' || !this._ownName(r) && !r.label) { score += 8; reasons.push('неизвестное устройство'); }
    const dup = r.same_device_as || all.some(o => o !== r && ((o.interfaces || []).some(i => i.mac === r.mac) || this._name(o) === this._name(r)));
    if (dup) { score += 25; reasons.push('дубликат: у устройства несколько резервов'); }
    return {score, reasons};
  }

  _replacePanel(d, row, busy) {
    const p = this._node('div', null, row, 'panel replace');
    const all = this._reserved().filter(r => r.mac !== d.mac);
    const cap = this._confirm.capacity || all.length;
    const choice = all.find(r => r.mac === this._confirm.choice);
    const dry = this._config.dry_run ? ' (проверка)' : '';
    if (choice) {
      // Step 2: confirm the swap.
      this._node('div', `Снять резервирование ${choice.reserved_ip} с «${this._name(choice)}» (${choice.mac}) и закрепить ${d.ip} за «${this._name(d)}» (${d.mac})?${dry}`, p, 'grow');
      this._node('div', 'Если новое резервирование не получится, старое будет восстановлено.', p, 'muted full');
      const yes = this._button('Заменить', 'mdi:swap-horizontal', p, 'primary', () => this._pinReplace(d, choice));
      yes.disabled = busy;
      this._button('Назад', 'mdi:arrow-left', p, '', () => { this._confirm = {...this._confirm, choice: null}; this._renderList(); });
      this._button('Отмена', null, p, '', () => { this._confirm = null; this._renderList(); });
      if (!busy) requestAnimationFrame(() => yes.focus());
      return;
    }
    this._node('div', `Все ${cap} слотов статических адресов заняты — заменить:`, p, 'rtitle full');
    const scored = all.map(r => ({r, ...this._replaceScore(r, all)})).sort((a, b) => b.score - a.score);
    const best = new Set(scored.filter(x => x.score >= 10 && !x.blocked).slice(0, 2).map(x => x.r.mac));
    const ul = this._node('div', null, p, 'rlist full');
    ul.setAttribute('role', 'listbox'); ul.setAttribute('aria-label', 'Какое резервирование освободить');
    for (const {r, reasons, blocked} of scored) {
      const b = this._node('button', null, ul, `ropt${best.has(r.mac) ? ' best' : ''}`);
      b.type = 'button'; b.setAttribute('role', 'option'); b.disabled = !!blocked || busy;
      const av = this._node('span', null, b, 'avatar sm');
      this._icon(this._iconFor(r), av);
      this._node('span', null, av, `dot${r.online ? ' on' : ''}`);
      const t = this._node('span', null, b, 'rt');
      this._node('span', this._name(r), t, 'rn');
      this._node('span', [r.reserved_ip, r.mac, r.online ? 'онлайн' : r.last_seen ? `был ${this._ago(r.last_seen)}` : 'в сети не замечен'].join(' · '), t, 'rm mono');
      if (reasons.length) this._node('span', reasons.join(' · '), t, 'rr');
      if (best.has(r.mac)) this._badge(b, 'лучше заменить', 'mdi:thumb-up-outline', 'new');
      b.addEventListener('click', (e) => { e.stopPropagation(); this._confirm = {...this._confirm, choice: r.mac}; this._renderList(); });
    }
    if (!all.length) this._node('div', 'Резервирований в списке нет — обновите данные роутера кнопкой «Обновить».', p, 'muted full');
    this._button('Отмена', null, p, '', () => { this._confirm = null; this._renderList(); });
  }

  _editPanel(d, row, busy) {
    const p = this._node('div', null, row, 'panel');
    const name = this._node('input', null, p, 'grow');
    const origName = d.friendly_name || d.display_name || this._plainName(d);
    name.value = origName; name.maxLength = 64; name.placeholder = 'Имя'; name.setAttribute('aria-label', 'Имя устройства');
    // Only a real icon from router-cli counts as the current value; the client-side guess is just
    // shown, so a plain rename doesn't freeze the heuristic icon as a permanent override.
    const origIcon = ND_MDI.test(d.icon || '') && d.icon === this._iconFor(d) ? d.icon : '';
    let chosen = origIcon;
    const wrap = this._node('div', null, p, 'pickwrap');
    const pickBtn = this._button('', null, wrap, 'pick', () => this._gallery(wrap, pickBtn, chosen, (v) => {
      chosen = v; pickBtn.firstChild.setAttribute('icon', v || this._iconFor({...d, icon: ''}));
      pickLbl.textContent = v ? v.slice(4) : 'авто';
    }));
    this._icon(chosen || this._iconFor(d), pickBtn);
    const pickLbl = this._node('span', chosen ? chosen.slice(4) : 'авто', pickBtn, 'plbl');
    this._icon('mdi:menu-down', pickBtn);
    pickBtn.setAttribute('aria-haspopup', 'dialog'); pickBtn.title = 'Выбрать иконку';
    const save = () => {
      const n = name.value.trim();
      const newName = n !== origName ? n : '';
      const newIcon = chosen !== origIcon ? chosen : '';
      if (!newName && !newIcon) { this._editing = null; this._renderList(); return; }
      this._alias(d, newName, newIcon);
    };
    name.addEventListener('keydown', e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') { this._editing = null; this._renderList(); } });
    this._button('Сохранить', 'mdi:content-save', p, 'primary', save).disabled = busy;
    this._button('Отмена', null, p, '', () => { this._editing = null; this._renderList(); });
    if (!busy) requestAnimationFrame(() => name.focus());
  }

  /* ---------- icon gallery ---------- */
  _gallery(wrap, anchor, current, onPick) {
    if (this._gal) { const same = this._gal.anchor === anchor; this._gal.close(false); if (same) return; }
    anchor.setAttribute('aria-expanded', 'true');
    // Fixed to the viewport (the card clips overflow): below the button, flipped above when there
    // is no room, a bottom sheet on phones; follows the button while the page scrolls.
    const g = this._node('div', null, this.shadowRoot, 'gallery');
    const place = () => {
      if (!anchor.isConnected) { close(false); return; }
      const r = anchor.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
      g.classList.toggle('sheet', vw < 600);
      if (vw < 600) { Object.assign(g.style, {left: '', top: '', bottom: '', width: '', maxHeight: ''}); return; }
      const w = Math.min(440, vw - 16);
      const below = vh - r.bottom - 12, above = r.top - 12;
      const up = below < 300 && above > below;
      Object.assign(g.style, {width: `${w}px`, left: `${Math.max(8, Math.min(vw - w - 8, r.right - w))}px`,
        top: up ? '' : `${r.bottom + 6}px`, bottom: up ? `${vh - r.top + 6}px` : '',
        maxHeight: `${Math.max(200, Math.min(400, up ? above : below))}px`});
    };
    const outside = (e) => { if (!e.composedPath().some(n => n === g || n === anchor)) close(false); };
    window.addEventListener('scroll', place, {capture: true, passive: true});
    window.addEventListener('resize', place);
    document.addEventListener('pointerdown', outside, true);
    g.setAttribute('role', 'dialog'); g.setAttribute('aria-label', 'Выбор иконки');
    const top = this._node('div', null, g, 'gtop');
    const q = this._node('input', null, top, 'grow');
    q.type = 'search'; q.placeholder = 'Поиск по всем иконкам MDI (англ.): phone, printer…';
    q.setAttribute('aria-label', 'Поиск иконки');
    const auto = this._button('Авто', 'mdi:auto-fix', top, '', () => pick(''));
    auto.title = 'Без переопределения: иконку выбирает router-cli';
    const body = this._node('div', null, g, 'gbody');
    const close = (refocus = true) => {
      g.remove(); anchor.setAttribute('aria-expanded', 'false');
      window.removeEventListener('scroll', place, {capture: true});
      window.removeEventListener('resize', place);
      document.removeEventListener('pointerdown', outside, true);
      if (this._gal?.el === g) this._gal = null;
      if (refocus && anchor.isConnected) anchor.focus();
    };
    this._gal = {el: g, anchor, close};
    const pick = (v) => { onPick(v); close(); };
    const grid = (parent, names) => {
      const gr = this._node('div', null, parent, 'grid');
      gr.setAttribute('role', 'listbox');
      for (const n of names) {
        const b = this._node('button', null, gr, `gi${n === current ? ' sel' : ''}`);
        b.type = 'button'; b.title = n.slice(4); b.setAttribute('role', 'option'); b.setAttribute('aria-label', n.slice(4));
        b.setAttribute('aria-selected', String(n === current));
        this._icon(n, b);
        b.addEventListener('click', (e) => { e.stopPropagation(); pick(n); });
      }
      return gr;
    };
    const curated = () => {
      body.replaceChildren();
      for (const [title, names] of ND_GALLERY) { this._node('div', title, body, 'gh'); grid(body, names); }
    };
    let seq = 0;
    const search = async () => {
      const s = q.value.trim().toLowerCase().replace(/^mdi:/, '');
      if (!s) { curated(); return; }
      const my = ++seq;
      const curatedHits = ND_GALLERY.flatMap(([, ns]) => ns).filter(n => n.includes(s));
      if (!ndIconList) {
        body.replaceChildren(this._node('div', 'Загрузка списка иконок…', null, 'gh'));
        try { ndIconList = await (await fetch('/static/mdi/iconList.json')).json(); } catch (e) { ndIconList = []; }
        if (my !== seq) return;
      }
      const hits = new Set(curatedHits);
      for (const it of ndIconList) {
        if (hits.size >= 160) break;
        if (it.name.includes(s) || (it.keywords || []).some(k => k.includes(s))) hits.add(`mdi:${it.name}`);
      }
      body.replaceChildren();
      if (!hits.size) { this._node('div', 'Ничего не найдено', body, 'gh'); return; }
      this._node('div', `Найдено: ${hits.size}${hits.size >= 160 ? '+' : ''}`, body, 'gh');
      grid(body, [...hits]);
    };
    let deb;
    q.addEventListener('input', () => { clearTimeout(deb); deb = setTimeout(search, 150); });
    // Keyboard: arrows move through the grid, Enter picks, Escape closes, ArrowDown from search enters the grid.
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
      const items = [...g.querySelectorAll('.gi')];
      const i = items.indexOf(this.shadowRoot.activeElement);
      if (e.target === q) { if (e.key === 'ArrowDown' && items.length) { e.preventDefault(); items[0].focus(); } return; }
      if (i < 0) return;
      const move = {ArrowRight: 1, ArrowLeft: -1}[e.key];
      if (move) { e.preventDefault(); const j = i + move; (j < 0 ? q : items[Math.min(j, items.length - 1)]).focus(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        // Nearest icon in the next/previous visual row (works across section headers).
        e.preventDefault();
        const r0 = items[i].getBoundingClientRect(), down = e.key === 'ArrowDown';
        let best = null, bestScore = Infinity;
        for (const it of items) {
          const r = it.getBoundingClientRect();
          const dy = down ? r.top - r0.top : r0.top - r.top;
          if (dy < 4) continue;
          const score = dy * 1000 + Math.abs(r.left - r0.left);
          if (score < bestScore) { bestScore = score; best = it; }
        }
        if (best) { best.focus(); best.scrollIntoView({block: 'nearest'}); } else if (!down) q.focus();
      }
    });
    curated();
    place();
    requestAnimationFrame(() => q.focus());
  }

  /* ---------- charts (inline SVG, single hue, HA theme vars) ---------- */
  _bucketsOf(mac) { const h = this._history.get(mac); return h && Array.isArray(h.buckets) ? h.buckets : null; }

  _paintHistory(mac) {
    const sp = this._list.querySelector(`.spark[data-mac="${CSS.escape(mac)}"]`);
    if (sp) this._paintSpark(sp, mac);
    if (this._drawerMac === mac) this._renderDrawer();
  }

  // Isolated values (no neighbour to draw a line to) get an 8px dot so they stay visible.
  _dots(parent, vals, max) {
    const n = vals.length;
    vals.forEach((v, i) => {
      if (v === null || (i > 0 && vals[i - 1] !== null) || (i < n - 1 && vals[i + 1] !== null)) return;
      const p = this._node('span', null, parent, 'pt');
      p.style.left = `${(i + 0.5) / n * 100}%`;
      p.style.top = `${(1 - v / (max || 1)) * 100}%`;
    });
  }

  _paintSpark(sp, mac) {
    const h = this._history.get(mac);
    const b = this._bucketsOf(mac);
    sp.replaceChildren();
    if (!b) { sp.classList.toggle('err', !!h?.error); sp.title = h?.error ? `История недоступна: ${h.error}` : ''; return; }
    if (!b.length) { sp.title = 'Нет истории'; return; }
    const vals = b.map(x => this._num(x.online_ratio) === null ? null : Math.max(0, Math.min(1, this._num(x.online_ratio))));
    const box = this._node('span', null, sp, 'sbox');
    const svg = this._svg('svg', {viewBox: `0 0 ${vals.length} 20`, preserveAspectRatio: 'none', 'aria-hidden': 'true'}, box);
    this._gaps(svg, vals, 20);
    this._area(svg, vals, 1, 20);
    this._dots(box, vals, 1);
    const avg = vals.filter(v => v !== null);
    const mean = avg.length ? Math.round(avg.reduce((a, v) => a + v, 0) / avg.length * 100) : null;
    sp.setAttribute('aria-label', `Онлайн по часам за ${this._config.history_days} дней${mean !== null ? `, в среднем ${mean}%` : ''}`);
    this._hover(box, vals.length, (i) => `${this._when(b[i].t)} — онлайн ${vals[i] === null ? this._noData(b[i]) : `${Math.round(vals[i] * 100)}%`}${this._samples(b[i], vals[i])}`);
    if (mean !== null) this._node('span', `${mean}%`, sp, 'sv').title = 'Средняя доля времени онлайн';
  }

  // Hours without any presence sweep (online_ratio/online null) are hatched, so a gap reads as
  // "not measured", never as "offline".
  _gaps(svg, vals, H) {
    let start = -1;
    const flush = (end) => { if (start >= 0) this._svg('rect', {x: start, y: 0, width: end - start, height: H, class: 'nd'}, svg); start = -1; };
    vals.forEach((v, i) => { if (v === null) { if (start < 0) start = i; } else flush(i); });
    flush(vals.length);
  }
  _noData(b) {
    const since = this._ts(this._stats?.since) || ND_PRESENCE_SINCE;
    return this._tms(b?.t) + 3600e3 <= since ? 'нет данных (история ещё не собиралась)' : 'нет данных (замеров не было)';
  }
  _samples(b, v) {
    const n = this._num(b?.samples);
    return n !== null && v !== null ? ` · ${n} ${this._plural(n, 'замер', 'замера', 'замеров')}` : '';
  }

  // Area + 2px line; null values break the path (no data ≠ zero).
  _area(svg, vals, max, H) {
    let line = '', fill = '', seg = [];
    const flush = () => {
      if (!seg.length) return;
      const pts = seg.map(([i, v]) => `${i + 0.5},${(H - (v / (max || 1)) * (H - 1)).toFixed(2)}`);
      if (seg.length === 1) pts.push(`${seg[0][0] + 0.9},${pts[0].split(',')[1]}`);
      line += `M${pts.join('L')}`;
      fill += `M${seg[0][0] + 0.5},${H}L${pts.join('L')}L${seg[seg.length - 1][0] + 0.5},${H}Z`;
      seg = [];
    };
    vals.forEach((v, i) => { if (v === null) flush(); else seg.push([i, v]); });
    flush();
    this._svg('path', {d: fill, class: 'af'}, svg);
    this._svg('path', {d: line, class: 'al', 'vector-effect': 'non-scaling-stroke'}, svg);
  }

  _when(t) {
    const d = new Date(this._tms(t));
    if (!Number.isFinite(d.getTime())) return '—';
    return `${ND_WD[d.getDay()]} ${d.toLocaleDateString('ru-RU', {day: 'numeric', month: 'short'})}, ${String(d.getHours()).padStart(2, '0')}:00`;
  }

  // Shared crosshair + tooltip for charts: one hit area over the whole plot.
  _hover(el, n, text, cross) {
    const at = (ev) => {
      const r = el.getBoundingClientRect();
      const i = Math.max(0, Math.min(n - 1, Math.floor((ev.clientX - r.left) / r.width * n)));
      if (cross) { cross.style.left = `${(i + 0.5) / n * 100}%`; cross.hidden = false; }
      this._tip.textContent = text(i);
      this._tip.classList.add('on');
      const tw = this._tip.offsetWidth;
      this._tip.style.left = `${Math.max(4, Math.min(window.innerWidth - tw - 4, ev.clientX - tw / 2))}px`;
      this._tip.style.top = `${Math.max(4, r.top - this._tip.offsetHeight - 6)}px`;
    };
    el.addEventListener('pointermove', at);
    el.addEventListener('pointerdown', at);
    el.addEventListener('pointerleave', () => { this._tip.classList.remove('on'); if (cross) cross.hidden = true; });
  }

  // A labelled time-series chart: y gridlines at 0 / half / max, day ticks at local midnight.
  _chart(parent, {title, buckets, value, max, fmt, tipLabel, height = 110}) {
    const box = this._node('figure', null, parent, 'chart');
    if (title) this._node('figcaption', title, box);
    const vals = buckets.map(value);
    const real = vals.filter(v => v !== null);
    const top = max ?? Math.max(1, ...real);
    const wrap = this._node('div', null, box, 'cw');
    wrap.style.height = `${height}px`;
    const yax = this._node('div', null, wrap, 'yax');
    const plot = this._node('div', null, wrap, 'plot');
    for (const f of [1, 0.5, 0]) {
      this._node('span', fmt(top * f), yax).style.top = `${(1 - f) * 100}%`;
      this._node('div', null, plot, `gl${f === 0 ? ' base' : ''}`).style.top = `${(1 - f) * 100}%`;
    }
    const xax = this._node('div', null, box, 'xax');
    buckets.forEach((b, i) => {
      const d = new Date(this._tms(b.t));
      if (d.getHours() === 0) {
        const pos = `${i / buckets.length * 100}%`;
        this._node('div', null, plot, 'vl').style.left = pos;
        const lb = this._node('span', `${ND_WD[d.getDay()]} ${d.getDate()}`, xax);
        lb.style.left = pos;
      }
    });
    const svg = this._svg('svg', {viewBox: `0 0 ${buckets.length} 100`, preserveAspectRatio: 'none', 'aria-hidden': 'true'}, plot);
    this._gaps(svg, vals, 100);
    this._area(svg, vals, top, 100);
    this._dots(plot, vals, top);
    const cross = this._node('div', null, plot, 'cross'); cross.hidden = true;
    const tbl = real.length ? `${title || ''}: максимум ${fmt(Math.max(...real))}, в среднем ${fmt(real.reduce((a, v) => a + v, 0) / real.length)}` : 'нет данных';
    plot.setAttribute('role', 'img'); plot.setAttribute('aria-label', tbl);
    this._hover(plot, buckets.length, (i) => `${this._when(buckets[i].t)} — ${tipLabel}: ${vals[i] === null ? this._noData(buckets[i]) : fmt(vals[i], true)}${this._samples(buckets[i], vals[i])}`, cross);
    return box;
  }

  _renderStats() {
    const box = this._statsBox;
    if (!box) return;
    const s = this._stats;
    // Appears only once the bridge has answered (no "loading" flash while the endpoint may not exist).
    const show = this._has('network_stats') && !!s;
    box.hidden = !show;
    if (!show) return;
    const sig = JSON.stringify([s, this._data?.devices?.length]);
    if (sig === this._statsSig) return;
    this._statsSig = sig;
    box.replaceChildren();
    const sum = this._node('summary', null, box);
    this._icon('mdi:chevron-right', sum, 'chev');
    this._node('span', `Активность за ${this._config.history_days} дней`, sum);
    if (!s) { this._node('div', 'Загрузка статистики…', box, 'muted pad'); return; }
    if (s.error) { this._node('div', `Статистика недоступна: ${s.error}`, box, 'muted pad'); return; }
    const kpis = this._node('div', null, box, 'kpis');
    const per = Array.isArray(s.per_hour) ? s.per_hour : [];
    const peak = per.reduce((m, p) => Math.max(m, Number(p.online) || 0), 0);
    const kpi = (label, value, hint) => {
      const k = this._node('div', null, kpis, 'kpi');
      this._node('div', value, k, 'kv');
      this._node('div', label, k, 'kl');
      if (hint) k.title = hint;
    };
    kpi('онлайн сейчас', String(s.online_now ?? '—'));
    kpi('в среднем', this._num(s.online_avg) !== null ? this._num(s.online_avg).toFixed(1).replace('.', ',') : '—', 'Среднее число устройств онлайн по часам');
    kpi('пик', per.some(p => this._num(p.online) !== null) ? String(Math.round(peak)) : '—', 'Максимум устройств онлайн за час (среднее по замерам внутри часа)');
    kpi('всего известно', String(this._data?.devices?.length ?? '—'));
    const notes = [];
    const since = this._ts(s.since);
    if (since && Date.now() - since < this._config.history_days * 86400e3) {
      notes.push(`История собирается с ${new Date(since).toLocaleString('ru-RU', {day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit'})} — раньше данных нет.`);
    }
    if (notes.length) this._node('div', notes.join(' '), box, 'muted');
    for (const c of (Array.isArray(s.ip_conflicts) ? s.ip_conflicts : []).filter(c => c && c.ip).slice(0, 5)) {
      const names = (c.macs || []).map(m => { const d = (this._data?.devices || []).find(x => x.mac === m); return d ? `${this._name(d)} (${m})` : m; });
      const w = this._node('div', null, box, 'conflict');
      this._icon('mdi:alert', w);
      this._node('span', `IP ${c.ip} переходил между устройствами: ${names.join(' и ')}${c.flips ? ` (${c.flips} раз)` : ''}. Закрепите IP за нужным устройством.`, w);
    }
    const grid = this._node('div', null, box, 'sgrid');
    if (per.length) {
      this._chart(grid, {title: 'Устройств онлайн по часам', buckets: per,
        value: (p) => this._num(p.online),
        max: Math.max(1, peak), fmt: (v) => String(Math.round(v)), tipLabel: 'онлайн', height: 120});
    }
    const top = (Array.isArray(s.top_traffic) ? s.top_traffic : []).filter(t => t && t.mac).slice(0, 6);
    if (top.length) {
      const fig = this._node('figure', null, grid, 'chart talkers');
      this._node('figcaption', 'Больше всего трафика', fig);
      const leg = this._node('div', null, fig, 'legend');
      const li = (cls, t) => { const x = this._node('span', null, leg); this._node('i', null, x, cls); this._node('span', t, x); };
      li('sw rx', 'получено'); li('sw tx', 'отправлено');
      const maxT = Math.max(1, ...top.map(t => (Number(t.rx_bytes) || 0) + (Number(t.tx_bytes) || 0)));
      for (const t of top) {
        const rx = Number(t.rx_bytes) || 0, tx = Number(t.tx_bytes) || 0;
        const r = this._node('button', null, fig, 'talker');
        r.type = 'button';
        const dev = (this._data?.devices || []).find(d => d.mac === t.mac);
        r.title = `${t.display_name || t.mac}: получено ${this._bytes(rx)}, отправлено ${this._bytes(tx)}`;
        r.addEventListener('click', () => this._openDrawer(t.mac));
        if (dev) this._icon(this._iconFor(dev), r, 'ti');
        this._node('span', t.display_name || (dev && this._name(dev)) || t.mac, r, 'tn');
        const bar = this._node('span', null, r, 'tb');
        this._node('i', null, bar, 'rx').style.width = `${rx / maxT * 100}%`;
        this._node('i', null, bar, 'tx').style.width = `${tx / maxT * 100}%`;
        this._node('span', this._bytes(rx + tx), r, 'tv');
      }
    }
  }

  /* ---------- device drawer ---------- */
  _openDrawer(mac) {
    this._drawerMac = mac; this._drawerFrom = this.shadowRoot.activeElement; this._drawerFocus = true;
    this._wantHistory(mac);
    this._renderDrawer();
  }
  _closeDrawer() {
    this._drawerMac = null; this._drawer.hidden = true; this._drawer.replaceChildren();
    this._tip.classList.remove('on');
    this._drawerFrom?.focus?.();
  }
  _renderDrawer() {
    const d = (this._data?.devices || []).find(x => x.mac === this._drawerMac);
    if (!d) { this._closeDrawer(); return; }
    const w = this._drawer;
    const scroll = w.querySelector('.drawer')?.scrollTop || 0;
    w.replaceChildren(); w.hidden = false;
    const back = this._node('div', null, w, 'scrim');
    back.addEventListener('click', () => this._closeDrawer());
    const dr = this._node('aside', null, w, 'drawer');
    dr.setAttribute('role', 'dialog'); dr.setAttribute('aria-modal', 'true'); dr.setAttribute('aria-label', this._name(d));
    const hd = this._node('div', null, dr, 'dhead');
    const av = this._node('div', null, hd, 'avatar');
    this._icon(this._iconFor(d), av);
    this._node('span', null, av, `dot${d.online ? ' on' : ''}`);
    const ttl = this._node('div', null, hd, 'dtitle');
    this._cp(this._node('div', this._name(d), ttl, 'name'), this._name(d), 'имя');
    const sub = this._node('div', null, ttl, 'muted mono dsub');
    for (const [v, what] of [[d.ip, 'IP'], [d.mac, 'MAC'], [d.vendor, 'производителя']]) if (v) this._cp(this._node('span', v, sub), v, what);
    const x = this._button('', 'mdi:close', hd, 'icon', () => this._closeDrawer());
    x.setAttribute('aria-label', 'Закрыть'); x.title = 'Закрыть (Esc)';

    const facts = this._node('div', null, dr, 'facts');
    const fact = (k, v, icon, copy) => {
      if (!v) return null;
      const r = this._node('div', null, facts, 'fact'); this._icon(icon || 'mdi:circle-small', r);
      this._node('span', k, r, 'k');
      const val = this._node('span', v, r, 'v');
      if (copy) this._cp(val, copy === true ? v : copy, k.toLowerCase());
      return r;
    };
    fact('Статус', d.is_self ? 'онлайн (этот сервер)' : d.online ? 'онлайн' : `офлайн, был ${this._ago(d.last_seen)}`, d.online ? 'mdi:lan-connect' : 'mdi:lan-disconnect');
    fact('Имя хоста', d.hostname, 'mdi:dns', true);
    if (d.label && d.label !== this._name(d)) fact('Опознано как', d.label, 'mdi:tag-outline', true);
    const withBrand = (s) => (d.brand && s && !s.toLowerCase().includes(String(d.brand).toLowerCase()) ? `${d.brand} ${s}` : s);
    const product = withBrand(d.model || d.product || '');
    if (product) fact('Модель', product, 'mdi:information-outline', true);
    fact('Где стоит', this._loc(d), 'mdi:map-marker', true);
    const other = (d.names || []).filter(n => n && n !== d.hostname && n !== this._name(d));
    if (other.length) fact('Другие имена', [...new Set(other)].slice(0, 4).join(', '), 'mdi:tag-multiple-outline', true);
    const ct = this._connText(d);
    if (ct) fact('Подключение', ct[1], ct[0]);
    if (d.category) fact('Категория', `${this._catLabel(d.category)}${this._conf(d) !== null ? ` · уверенность ${this._conf(d)}%` : ''}`, 'mdi:shape-outline');
    const tr = this._traffic(d);
    if (tr) {
      fact('Трафик', `↓ ${this._bytes(tr.rx)} · ↑ ${this._bytes(tr.tx)}`, 'mdi:swap-vertical');
      const rate = this._rate(tr.rxr + tr.txr);
      if (rate) fact('Сейчас', `↓ ${this._rate(tr.rxr) || '0'} · ↑ ${this._rate(tr.txr) || '0'}`, 'mdi:speedometer');
    }
    fact('Впервые', d.first_seen ? new Date(this._ts(d.first_seen)).toLocaleString('ru-RU') : '', 'mdi:calendar-start');
    if (d.reserved_ip) fact('DHCP', `закреплён ${d.reserved_ip}`, 'mdi:pin', d.reserved_ip);
    else if (!this._pinnable(d)) fact('DHCP', 'приватный MAC — закреплять IP бесполезно', 'mdi:incognito');

    const ifs = this._ifaces(d);
    if (ifs.length > 1 || ifs[0]?.name) {
      this._node('div', `Сетевые интерфейсы (${ifs.length})`, dr, 'dsec');
      const box = this._node('div', null, dr, 'ifs col');
      for (const i of ifs) this._ifaceChip(box, i);
    }

    const h = this._history.get(d.mac);
    const b = this._bucketsOf(d.mac);
    const charts = this._node('div', null, dr, 'dcharts');
    if (!this._has('network_history')) this._node('div', 'История появится, когда router-cli начнёт её отдавать (rest_command.network_history).', charts, 'muted');
    else if (!b) this._node('div', h?.error ? `История недоступна: ${h.error}` : 'Загрузка истории…', charts, 'muted');
    else if (!b.length) this._node('div', 'Истории пока нет.', charts, 'muted');
    else {
      const since = this._ts(this._stats?.since) || ND_PRESENCE_SINCE;
      if (b.length && this._tms(b[0].t) < since) {
        this._node('div', `История присутствия собирается с ${new Date(since).toLocaleString('ru-RU', {day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit'})}; заштрихованные часы — нет данных, а не «офлайн».`, charts, 'muted');
      }
      this._chart(charts, {title: 'Онлайн, % времени в час', buckets: b, max: 1,
        value: (x) => this._num(x.online_ratio) === null ? null : Math.max(0, Math.min(1, this._num(x.online_ratio))),
        fmt: (v) => `${Math.round(v * 100)}%`, tipLabel: 'онлайн'});
      const num = (k) => (x) => this._num(x[k]);
      const tmax = Math.max(1, ...b.map(x => Math.max(Number(x.rx_bytes) || 0, Number(x.tx_bytes) || 0)));
      if (b.some(x => x.rx_bytes !== null && x.rx_bytes !== undefined)) {
        // Two small multiples on one shared scale instead of two colors on one plot.
        this._chart(charts, {title: 'Получено за час', buckets: b, value: num('rx_bytes'), max: tmax, fmt: (v) => this._bytes(v), tipLabel: 'получено'});
        this._chart(charts, {title: 'Отправлено за час', buckets: b, value: num('tx_bytes'), max: tmax, fmt: (v) => this._bytes(v), tipLabel: 'отправлено'});
      }
    }
    if (d.category || (d.evidence || []).length || this._randomMac(d)) {
      const was = this._open.has(d.mac); this._open.add(d.mac);
      this._why(dr, d);
      if (!was) this._open.delete(d.mac);
    }
    const svcs = (d.services || []).map(s => [s, this._serviceUrl(s, d)]).filter(([, u]) => u);
    if (svcs.length) {
      this._node('div', 'Веб-интерфейсы', dr, 'dsec');
      const box = this._node('div', null, dr, 'svcs');
      for (const [s, url] of svcs) this._svcChip(box, d, s, url);
    }
    const ips = (d.ip_history || []).filter(v => v && typeof v === 'object' && v.ip);
    if (ips.length > 1) {
      this._node('div', 'История IP', dr, 'dsec');
      const ul = this._node('ul', null, dr, 'iph');
      for (const v of ips.slice(-8).reverse()) { const li = this._node('li', null, ul, 'mono'); this._cp(this._node('span', v.ip, li), v.ip, 'IP'); this._node('span', ` — ${this._ago(v.last_seen)}`, li); }
    }
    dr.scrollTop = scroll;
    if (this._drawerFocus) { this._drawerFocus = false; requestAnimationFrame(() => x.focus()); }
  }

  /* ---------- mock (config mock: true) ---------- */
  _rng(seed) {
    let h = 2166136261;
    for (const ch of String(seed)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
    return () => { h ^= h << 13; h ^= h >>> 17; h ^= h << 5; return ((h >>> 0) % 100000) / 100000; };
  }
  _mockInventory(data) {
    const iconCat = {'mdi:cellphone': 'phone', 'mdi:tablet': 'tablet', 'mdi:laptop': 'laptop', 'mdi:desktop-tower-monitor': 'desktop',
      'mdi:home-assistant': 'server', 'mdi:router-network': 'router', 'mdi:television': 'tv', 'mdi:speaker': 'speaker', 'mdi:printer': 'printer',
      'mdi:printer-3d': 'printer_3d', 'mdi:cctv': 'camera', 'mdi:gamepad-variant': 'console', 'mdi:watch': 'watch', 'mdi:robot-vacuum': 'vacuum',
      'mdi:chip': 'esp', 'mdi:nas': 'nas', 'mdi:server': 'server', 'mdi:incognito': 'phone'};
    const devices = (data.devices || []).map(x => ({...x}));
    const gear = devices.filter(d => /ubee|tp-?link|keenetic|zyxel|deco|mesh/i.test(`${d.vendor} ${d.hostname}`) || d.ip === '192.168.0.1').slice(0, 2);
    if (gear.length < 2) for (const d of devices) { if (gear.length >= 2) break; if (!gear.includes(d) && !this._randomMac(d) && d.online) gear.push(d); }
    const nodeNames = ['Гостиная', 'Кабинет'];
    gear.forEach((g, i) => { g.is_network_gear = true; g.category = i ? 'mesh' : 'router'; g.display_name = g.display_name || `Mesh ${nodeNames[i]}`; g.icon = i ? 'mdi:access-point-network' : 'mdi:router-wireless'; });
    for (const d of devices) {
      const r = this._rng(d.mac);
      if (!d.category) {
        const ic = this._iconFor({...d, category: null});
        d.category = iconCat[ic] || (r() < 0.5 ? 'iot' : 'unknown');
        d.icon = ND_CATS[d.category]?.[1] || ic;
      }
      d.confidence = d.confidence ?? Math.round(40 + r() * 58) / 100;
      d.evidence = d.evidence ?? [
        d.vendor && {source: 'oui', detail: `Производитель по MAC: ${d.vendor}`, weight: 0.5},
        d.hostname && {source: 'hostname', detail: `Имя в DHCP: ${d.hostname}`, weight: 0.35},
        (d.services || []).length && {source: 'http', detail: `Веб-интерфейс на порту ${d.services[0].port}${d.services[0].server ? ` (${d.services[0].server})` : ''}`, weight: 0.3},
        this._randomMac(d) && {source: 'mac', detail: 'Локально администрируемый MAC (приватный адрес)', weight: 0.2},
      ].filter(Boolean);
      d.display_name = d.display_name ?? (d.hostname || (d.names || [])[0] || (d.vendor ? `${this._catLabel(d.category)} ${d.vendor.split(' ')[0]}` : null));
      d.pinnable = d.pinnable ?? !this._randomMac(d);
      d.is_network_gear = !!d.is_network_gear;
      if (!d.connection) {
        const wired = d.is_network_gear || ['server', 'nas', 'desktop', 'printer_3d', 'tv'].includes(d.category) && r() < 0.7;
        const via = gear.length ? gear[Math.floor(r() * gear.length)] : null;
        d.connection = d === gear[0] ? {type: 'wired', via: null, via_name: null, band: null, rssi: null}
          : wired ? {type: 'wired', via: via?.mac || null, via_name: via ? this._name(via) : null, band: null, rssi: null}
          : {type: 'wifi', via: via?.mac || null, via_name: via ? this._name(via) : null, band: r() < 0.6 ? '5' : '2.4', rssi: -Math.round(38 + r() * 45)};
      }
      if (d.traffic === undefined) d.traffic = r() < 0.15 ? null : {rx_bytes: Math.round(r() ** 3 * 8e9), tx_bytes: Math.round(r() ** 3 * 1.5e9),
        rx_rate: d.online ? Math.round(r() ** 4 * 3e6) : 0, tx_rate: d.online ? Math.round(r() ** 4 * 4e5) : 0, updated_at: data.last_poll, source: 'mock'};
      d.services = (d.services || []).map((s, i) => {
        if (s.reachable !== undefined) return s;
        const v = this._rng(`${d.mac}${s.port}`)();
        return v < 0.12 ? {...s, reachable: false, http_status: null, error: 'timeout'}
          : v < 0.25 ? {...s, reachable: true, http_status: i % 2 ? 401 : 502, error: null}
          : v < 0.32 ? {...s, reachable: true, http_status: null, error: 'TLS: self-signed certificate'}
          : {...s, reachable: true, http_status: 200, error: null};
      });
    }
    this._mockDevices = devices;
    return {...data, devices};
  }
  _mockHours(days) {
    const now = new Date(); now.setMinutes(0, 0, 0);
    const n = days * 24;
    return Array.from({length: n}, (_, i) => new Date(now.getTime() - (n - 1 - i) * 3600e3));
  }
  async _mockService(service, data) {
    await new Promise(r => setTimeout(r, 150 + Math.random() * 250));
    const days = Number(data.days) || 7;
    const hours = this._mockHours(days);
    const devs = this._mockDevices || [];
    const ratio = (d, t) => {
      const r = this._rng(`${d.mac}${t.getTime()}`)();
      const h = t.getHours();
      const base = d.is_network_gear || ['server', 'nas', 'esp', 'iot', 'speaker'].includes(d.category) ? 0.97
        : ['phone', 'watch'].includes(d.category) ? (h >= 8 && h <= 23 ? 0.85 : 0.35)
        : (h >= 18 && h <= 23 ? 0.8 : 0.15);
      if (Date.now() - this._ts(d.first_seen) < Date.now() - t.getTime()) return null;
      return r < base ? (r < base * 0.8 ? 1 : Math.round(r * 100) / 100) : 0;
    };
    if (service === 'network_history') {
      const d = devs.find(x => x.mac === data.device) || {mac: data.device};
      const rr = this._rng(`${d.mac}-t`);
      const scale = rr() ** 2 * 2e8;
      return {mac: d.mac, buckets: hours.map(t => {
        const o = ratio(d, t);
        return {t: t.toISOString(), online_ratio: o, rx_bytes: o === null ? null : Math.round(o * scale * rr()), tx_bytes: o === null ? null : Math.round(o * scale * 0.2 * rr())};
      })};
    }
    const per = hours.map(t => ({t: t.toISOString(), online: devs.reduce((a, d) => a + ((ratio(d, t) || 0) > 0.5 ? 1 : 0), 0)}));
    const top = devs.filter(d => d.traffic).sort((a, b) => (b.traffic.rx_bytes + b.traffic.tx_bytes) - (a.traffic.rx_bytes + a.traffic.tx_bytes)).slice(0, 5)
      .map(d => ({mac: d.mac, display_name: this._name(d), rx_bytes: d.traffic.rx_bytes, tx_bytes: d.traffic.tx_bytes}));
    return {online_now: devs.filter(d => d.online).length, online_avg: per.reduce((a, p) => a + p.online, 0) / per.length, per_hour: per, top_traffic: top};
  }

  static getStubConfig() { return {default_filter: 'recent'}; }
}

const ND_CSS = `
  :host{display:block;container-type:inline-size;--nd-copy:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 24 24%27%3E%3Cpath d=%27M19,21H8V7H19M19,5H8A2,2 0 0,0 6,7V21A2,2 0 0,0 8,23H19A2,2 0 0,0 21,21V7A2,2 0 0,0 19,5M16,1H4A2,2 0 0,0 2,3V17H4V3H16V1Z%27/%3E%3C/svg%3E");--nd-tx:color-mix(in srgb,var(--primary-color) 40%,var(--card-background-color,#fff))}
  ha-card{padding:16px 16px 8px;overflow:hidden}
  ha-icon{display:inline-flex;align-items:center;justify-content:center;flex:none;--mdc-icon-size:18px;width:var(--mdc-icon-size);height:var(--mdc-icon-size);line-height:0}
  .head{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap}
  .head h2{margin:0;font-size:20px;font-weight:600;line-height:1.3;display:flex;align-items:center;flex-wrap:wrap;gap:8px}
  .titlebox{flex:1;min-width:200px}
  .muted{color:var(--secondary-text-color);font-size:13px;line-height:1.5}
  .pad{padding:8px 4px}
  .mono{font-family:var(--code-font-family,monospace);font-size:12.5px}
  .test{display:inline-flex;align-items:center;padding:0 8px;height:20px;border-radius:10px;font-size:12px;font-weight:500;
    background:var(--warning-color,#ff9800);color:#fff}
  .headbtns{display:flex;gap:6px;align-items:center}
  button{font:inherit;font-size:13px;line-height:18px;min-height:36px;padding:4px 12px;border-radius:18px;cursor:pointer;
    border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);
    display:inline-flex;align-items:center;justify-content:center;gap:6px;box-sizing:border-box}
  button:hover{background:var(--secondary-background-color)}
  button:disabled{opacity:.5;cursor:default}
  button.primary{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}
  button.danger{color:var(--error-color)}
  button.icon{padding:0;width:36px}
  .spin ha-icon{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 10px}
  .chip{min-height:32px;border-radius:16px;padding:0 12px}
  .chip ha-icon{--mdc-icon-size:16px}
  .chip .n{color:var(--secondary-text-color);font-size:12px}
  .chip[aria-pressed=true]{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}
  .chip[aria-pressed=true] .n{color:inherit;opacity:.85}
  .tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px}
  .tools .toggle{min-height:36px}
  input,select{font:inherit;font-size:14px;min-height:36px;box-sizing:border-box;padding:4px 10px;border-radius:8px;
    border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color)}
  input.search{flex:1;min-width:180px}
  [hidden]{display:none!important}
  .error{color:var(--error-color);font-size:14px;margin:8px 0}
  .error:empty{display:none}

  details>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:4px;user-select:none}
  details>summary::-webkit-details-marker{display:none}
  details>summary .chev{transition:transform .15s}
  details[open]>summary .chev{transform:rotate(90deg)}

  .stats{margin:12px 0 0;border:1px solid var(--divider-color);border-radius:12px;padding:8px 12px}
  .stats>summary{font-size:14px;font-weight:600;min-height:28px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:8px 0 4px}
  .conflict{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--warning-color,#ff9800);margin-top:4px}
  .conflict ha-icon{--mdc-icon-size:16px}
  .conflict span{color:var(--primary-text-color)}
  .kpi{padding:6px 10px;border-radius:10px;background:var(--secondary-background-color)}
  .kv{font-size:22px;font-weight:600;line-height:1.2;font-variant-numeric:tabular-nums}
  .kl{font-size:12px;color:var(--secondary-text-color)}
  .sgrid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:8px 20px;align-items:start}
  .sgrid>:only-child{grid-column:1 / -1}
  .pt{position:absolute;width:8px;height:8px;margin:-4px 0 0 -4px;border-radius:50%;background:var(--primary-color);
    box-shadow:0 0 0 2px var(--card-background-color,#fff);pointer-events:none}
  figure{margin:0}
  .chart{margin:8px 0 4px;min-width:0}
  .chart figcaption{font-size:13px;font-weight:500;margin-bottom:6px}
  .cw{position:relative;display:flex}
  .yax{position:relative;width:44px;flex:none;font-size:11px;color:var(--secondary-text-color)}
  .yax span{position:absolute;right:6px;transform:translateY(-50%);white-space:nowrap;font-variant-numeric:tabular-nums}
  .plot{position:relative;flex:1;min-width:0;cursor:crosshair;touch-action:pan-y}
  .plot svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
  .plot .gl{position:absolute;left:0;right:0;border-top:1px solid var(--divider-color);opacity:.6}
  .plot .gl.base{opacity:1}
  .plot .vl{position:absolute;top:0;bottom:0;border-left:1px dashed var(--divider-color);opacity:.5}
  .plot .cross{position:absolute;top:0;bottom:0;border-left:1px solid var(--secondary-text-color);pointer-events:none}
  .xax{position:relative;height:16px;margin-left:44px;font-size:11px;color:var(--secondary-text-color)}
  .xax span{position:absolute;top:2px;white-space:nowrap;padding-left:3px}
  .af{fill:var(--primary-color);fill-opacity:.18}
  .al{fill:none;stroke:var(--primary-color);stroke-width:2;stroke-linejoin:round}
  .legend{display:flex;gap:12px;font-size:12px;color:var(--secondary-text-color);margin-bottom:4px}
  .legend>span{display:inline-flex;align-items:center;gap:5px}
  .sw{display:inline-block;width:10px;height:10px;border-radius:2px}
  .sw.rx,.tb .rx{background:var(--primary-color)}
  .sw.tx,.tb .tx{background:var(--nd-tx)}
  .talker{display:grid;grid-template-columns:18px minmax(0,1.2fr) minmax(40px,1fr) auto;align-items:center;gap:8px;width:100%;
    min-height:30px;padding:2px 6px;border:0;border-radius:8px;background:none;text-align:left}
  .talker .ti{--mdc-icon-size:16px;color:var(--secondary-text-color)}
  .talker .tn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tb{display:flex;gap:2px;height:10px;align-items:stretch}
  .tb i{display:block;border-radius:0 3px 3px 0;min-width:2px}
  .tb i.rx{border-radius:3px 0 0 3px}
  .tv{font-size:12px;color:var(--secondary-text-color);font-variant-numeric:tabular-nums;text-align:right;min-width:56px}

  .list{display:flex;flex-direction:column}
  .group{display:flex;align-items:center;gap:8px;padding:14px 4px 6px;font-size:13px;font-weight:600;color:var(--secondary-text-color);
    border-top:1px solid var(--divider-color)}
  .group ha-icon{--mdc-icon-size:18px}
  .group .n{font-weight:400}
  .row{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:4px 12px;padding:10px 4px;
    border-top:1px solid var(--divider-color);align-items:start;content-visibility:auto;contain-intrinsic-size:auto 110px}
  .row.child{margin-left:28px;padding-left:10px;border-left:2px solid var(--divider-color)}
  .row.gear{background:linear-gradient(90deg,rgba(var(--rgb-primary-color,3,169,244),.07),transparent 60%);
    box-shadow:inset 3px 0 0 var(--primary-color)}
  .row.offline .avatar ha-icon{opacity:.45}
  .avatar{position:relative;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex:none;
    background:var(--secondary-background-color);color:var(--state-icon-color,var(--primary-color))}
  .avatar ha-icon{--mdc-icon-size:24px}
  .dot{position:absolute;right:0;bottom:0;width:11px;height:11px;border-radius:50%;box-sizing:border-box;
    border:2px solid var(--card-background-color);background:var(--disabled-text-color,#9e9e9e)}
  .dot.on{background:var(--success-color,#43a047)}
  .main{min-width:0}
  .name{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;min-height:24px}
  .nm{font-size:15px;font-weight:600;line-height:1.35;overflow-wrap:anywhere;
    background:none;color:var(--primary-text-color)}

  .line{font-size:13px;line-height:20px;color:var(--secondary-text-color);display:flex;flex-wrap:wrap;align-items:center;gap:2px 10px;margin-top:2px}
  .line>span{display:inline-flex;align-items:center;gap:4px}
  .ip{color:var(--primary-text-color);font-size:13.5px}
  .badge{display:inline-flex;align-items:center;gap:4px;height:20px;padding:0 8px;border-radius:10px;font-size:11.5px;line-height:1;
    background:var(--secondary-background-color);color:var(--secondary-text-color);white-space:nowrap;box-sizing:border-box}
  .badge ha-icon{--mdc-icon-size:14px}
  .badge.pin,.badge.gearb{background:rgba(var(--rgb-primary-color,3,169,244),.15);color:var(--primary-color)}
  .badge.warn{background:rgba(var(--rgb-warning-color,255,152,0),.18);color:var(--warning-color,#ff9800)}
  .badge.new{background:rgba(var(--rgb-success-color,67,160,71),.18);color:var(--success-color,#43a047)}
  .badge.cat{background:none;border:1px solid var(--divider-color)}
  .badge.conn{color:var(--primary-text-color)}
  .badge.conn.guess{background:none;border:1px dashed var(--divider-color);color:var(--secondary-text-color)}
  .traffic{font-variant-numeric:tabular-nums}
  .traffic ha-icon{--mdc-icon-size:14px}
  .traffic .rate{color:var(--primary-text-color)}
  .spark{position:relative;display:inline-flex;align-items:center;gap:6px;height:20px;cursor:crosshair}
  .sbox{position:relative;display:block;width:112px;height:18px;border-bottom:1px solid var(--divider-color)}
  .sbox svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
  .spark:empty{width:112px;border-bottom:1px dashed var(--divider-color);height:18px}
  .spark .sv{font-size:12px;font-variant-numeric:tabular-nums}
  .svcs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  a.svc{display:inline-flex;align-items:center;gap:6px;max-width:100%;height:28px;box-sizing:border-box;padding:0 10px 0 7px;border-radius:14px;
    border:1px solid var(--divider-color);color:var(--primary-text-color);text-decoration:none;font-size:12.5px;line-height:1}
  a.svc:hover{background:var(--secondary-background-color);border-color:var(--primary-color)}
  a.svc img,a.svc ha-icon{width:16px;height:16px;--mdc-icon-size:16px;flex:none;border-radius:3px;display:block}
  a.svc ha-icon{display:inline-flex}
  a.svc .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
  a.svc .p{color:var(--secondary-text-color);font-family:var(--code-font-family,monospace);font-size:11.5px}
  a.svc.error ha-icon.st{color:var(--warning-color,#ff9800)}
  a.svc.down ha-icon.st{color:var(--error-color,#db4437)}
  a.svc.down .t{color:var(--secondary-text-color)}
  .why{margin-top:6px;font-size:13px}
  .why>summary{color:var(--secondary-text-color);min-height:24px}
  .why>summary ha-icon{--mdc-icon-size:16px}
  .whybody{padding:4px 0 2px 20px;display:flex;flex-direction:column;gap:4px}
  .ev{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:3px}
  .ev li{display:grid;grid-template-columns:minmax(70px,auto) minmax(0,1fr) 48px;gap:8px;align-items:center}
  .ev .src{font-family:var(--code-font-family,monospace);font-size:11.5px;color:var(--secondary-text-color)}
  .ev .w{height:6px;border-radius:3px;background:var(--divider-color);overflow:hidden}
  .ev .wf{display:block;height:100%;background:var(--primary-color);border-radius:3px}
  .acts{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end;align-items:center}
  .panel{grid-column:2 / -1;display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 10px;margin-top:4px;
    border-radius:10px;background:var(--secondary-background-color);font-size:13px}
  .panel .grow{min-width:140px;flex:1}
  .pickwrap{position:relative}
  .pick{border-radius:8px;padding:0 8px}
  .pick>ha-icon:first-child{--mdc-icon-size:22px}
  .pick .plbl{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--secondary-text-color)}
  .gallery{position:fixed;z-index:15;display:flex;flex-direction:column;
    background:var(--card-background-color,var(--ha-card-background,#fff));color:var(--primary-text-color);
    border:1px solid var(--divider-color);border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.25);overflow:hidden}
  .gallery.sheet{left:0;right:0;bottom:0;max-height:70vh;border-radius:16px 16px 0 0}
  .gtop{display:flex;gap:6px;padding:8px;border-bottom:1px solid var(--divider-color);align-items:center}
  .gtop .grow{flex:1;min-width:0}
  .gbody{overflow:auto;padding:4px 8px 8px;overscroll-behavior:contain}
  .gh{font-size:12px;font-weight:600;color:var(--secondary-text-color);margin:8px 2px 4px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(44px,1fr));gap:4px}
  .gi{min-height:44px;height:44px;padding:0;border-radius:10px;border-color:transparent;background:none}
  .gi ha-icon{--mdc-icon-size:24px}
  .gi:hover,.gi:focus-visible{background:var(--secondary-background-color);border-color:var(--divider-color)}
  .gi.sel{background:rgba(var(--rgb-primary-color,3,169,244),.18);border-color:var(--primary-color)}
  .empty{padding:24px 4px;text-align:center;color:var(--secondary-text-color)}
  .more{margin:8px auto 0;display:flex}
  .foot{padding:8px 4px 4px;font-size:12px;color:var(--secondary-text-color)}
  /* Text is selectable everywhere (HA's hui-root sets user-select:none on the whole view). */
  ha-card,.drawer,.tip{user-select:text;-webkit-user-select:text}
  button,summary,.chips,.gallery{user-select:none;-webkit-user-select:none}
  /* Click-to-copy values: dotted underline + copy glyph on hover, flash when copied. */
  .cp{cursor:copy;position:relative;border-radius:3px;text-decoration:underline dotted transparent;text-underline-offset:3px;
    transition:background-color .15s,text-decoration-color .15s}
  .cp:hover,.cp:focus-visible{text-decoration-color:currentColor;background:rgba(var(--rgb-primary-color,3,169,244),.08)}
  span.cp::after,div.cp::after{content:'';display:inline-block;width:12px;height:12px;margin-left:3px;vertical-align:-1px;background:currentColor;opacity:0;
    -webkit-mask:var(--nd-copy) center/contain no-repeat;mask:var(--nd-copy) center/contain no-repeat;transition:opacity .15s}
  span.cp:hover::after,div.cp:hover::after,.cp:focus-visible::after{opacity:.6}
  .cp.copied{background:rgba(var(--rgb-success-color,67,160,71),.2)}
  .badge.cp::after{display:none}
  button.avatar{padding:0;min-height:0;border:0;cursor:pointer}
  button.avatar:hover{background:rgba(var(--rgb-primary-color,3,169,244),.15)}
  .avatar.sm{width:30px;height:30px}
  .avatar.sm ha-icon{--mdc-icon-size:18px}
  .avatar.sm .dot{width:9px;height:9px}
  .host{color:var(--secondary-text-color)}
  .ifs{display:flex;flex-wrap:wrap;gap:4px 8px;margin-top:4px;font-size:12.5px}
  .ifs.col{flex-direction:column;align-items:flex-start;margin-bottom:6px}
  .iface{display:inline-flex;align-items:center;gap:6px;padding:1px 8px;border-radius:10px;border:1px solid var(--divider-color);color:var(--primary-text-color)}
  .iface ha-icon{--mdc-icon-size:14px;color:var(--secondary-text-color)}
  .iface .in{color:var(--secondary-text-color)}
  .iface.off{opacity:.6}
  .svcw{display:inline-flex;align-items:center;max-width:100%}
  button.svccp{min-height:0;width:24px;height:24px;padding:0;margin-left:2px;border:0;border-radius:12px;background:none;color:var(--secondary-text-color);opacity:.55}
  .svcw:hover button.svccp,button.svccp:focus-visible{opacity:1}
  button.svccp ha-icon{--mdc-icon-size:14px}
  .nd{fill:var(--secondary-text-color);fill-opacity:.08}
  .dsub{display:flex;flex-wrap:wrap;gap:2px 10px}
  .panel .full{flex-basis:100%}
  .rtitle{font-weight:600}
  .rlist{display:flex;flex-direction:column;gap:4px}
  button.ropt{display:flex;align-items:center;justify-content:flex-start;gap:10px;width:100%;min-height:44px;padding:6px 10px;border-radius:10px;text-align:left}
  button.ropt.best{border-color:var(--success-color,#43a047);background:rgba(var(--rgb-success-color,67,160,71),.08)}
  .rt{display:flex;flex-direction:column;min-width:0;flex:1}
  .rn{font-weight:600}
  .rm{font-size:12px;color:var(--secondary-text-color)}
  .rr{font-size:12px;color:var(--warning-color,#ff9800)}
  :focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}

  .tip{position:fixed;z-index:20;pointer-events:none;padding:4px 8px;border-radius:6px;font-size:12px;line-height:1.4;white-space:nowrap;
    background:var(--primary-text-color);color:var(--card-background-color,#fff);opacity:0;transition:opacity .08s;top:0;left:0}
  .tip.on{opacity:.95}
  .drawer-wrap{position:fixed;inset:0;z-index:10}
  .scrim{position:absolute;inset:0;background:rgba(0,0,0,.35)}
  .drawer{position:absolute;top:0;right:0;bottom:0;width:min(560px,100vw);box-sizing:border-box;overflow:auto;padding:16px;
    background:var(--card-background-color,var(--ha-card-background,#fff));color:var(--primary-text-color);
    box-shadow:-8px 0 24px rgba(0,0,0,.25);overscroll-behavior:contain}
  .dhead{display:flex;align-items:center;gap:12px;margin-bottom:12px}
  .dtitle{flex:1;min-width:0}
  .dtitle .name{font-size:17px;font-weight:600}
  .facts{display:flex;flex-direction:column;gap:2px;margin-bottom:8px}
  .fact{display:grid;grid-template-columns:20px 110px minmax(0,1fr);gap:8px;align-items:center;font-size:13px;min-height:26px}
  .fact ha-icon{color:var(--secondary-text-color)}
  .fact .k{color:var(--secondary-text-color)}
  .dsec{font-size:13px;font-weight:600;margin:12px 0 4px}
  .iph{margin:0;padding-left:18px;font-size:12.5px;color:var(--secondary-text-color)}
  .dcharts .chart{margin-bottom:14px}

  @container (max-width:700px){ .sgrid{grid-template-columns:minmax(0,1fr)} }
  @container (max-width:600px){
    ha-card{padding:12px 12px 6px}
    .row{grid-template-columns:40px minmax(0,1fr)}
    .row.child{margin-left:12px}
    .acts{grid-column:2 / -1;justify-content:flex-start}
    .panel{grid-column:1 / -1}
    a.svc .t{max-width:160px}
    .headbtns{width:100%}
  }
`;

if (!customElements.get('network-devices-card')) customElements.define('network-devices-card', NetworkDevicesCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'network-devices-card')) {
  window.customCards.push({type: 'network-devices-card', name: 'Network devices', description: 'Router inventory with topology, activity, web UIs and DHCP pinning (router-cli)'});
}
