// SPDX-License-Identifier: MIT
// Companion view: Adaptive Plant owns its settings; Recorder owns the measurements.
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const words = {ok:'Показания влажности',checking:'Проверяю сухость',due:'Пора проверить грунт',overdue:'Полив просрочен',snoozed:'Напоминания на паузе',sensor_error:'Нет свежих данных'};
  const forecastWords = {insufficient_data:'Недостаточно данных для оценки',saturated:'Индекс насыщен: оценить высыхание нельзя',unstable:'Показания нестабильны: оценка недоступна',stale:'Нет свежих данных для оценки',no_drying_trend:'Устойчивого высыхания пока не видно'};
  const dateLabel = value => {
    if (value == null || value === '') return null;
    const dateOnly = typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
    const d = new Date(dateOnly ? `${value}T12:00:00` : typeof value === 'number' ? value * 1000 : value);
    const options = dateOnly ? {day:'numeric',month:'long'} : {day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'};
    return Number.isFinite(d.getTime()) ? new Intl.DateTimeFormat('ru-RU',options).format(d) : null;
  };
  const ageLabel = value => {
    const ms = typeof value === 'number' ? value * 1000 : Date.parse(value);
    if (!Number.isFinite(ms) || ms > Date.now() + 60000) return 'возраст неизвестен';
    const minutes = Math.floor((Date.now() - ms) / 60000);
    if (minutes < 1) return 'только что';
    if (minutes < 60) return `${minutes} мин назад`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} ч назад`;
    return `${Math.floor(minutes / 1440)} дн назад`;
  };
  const imageURL = (value, version) => {
    if (!value) return '';
    try {
      const url = new URL(value, location.origin);
      if (!['http:','https:'].includes(url.protocol) || url.username || url.password) return '';
      if (version && url.origin === location.origin) url.searchParams.set('plant_care_revision', version);
      return url.href;
    } catch (_) { return ''; }
  };
  const number = value => value === null || value === undefined || (typeof value === 'string' && !value.trim()) ? null : Number.isFinite(Number(value)) ? Number(value) : null;

  class PlantCareCard extends HTMLElement {
    constructor() { super(); this.attachShadow({mode:'open'}); this._config = {}; this._history = {}; this._days = 7; this._pending = new Set(); }
    setConfig(config) { this._config = config || {}; this._render(); }
    static getStubConfig() { return {type:'custom:plant-care-card'}; }
    getCardSize() { return 6; }
    getGridOptions() { return {columns:12,min_columns:6,rows:'auto'}; }
    set hass(hass) { this._hass = hass; this._loadHistory(); this._render(); }
    get hass() { return this._hass; }
    _plants() {
      return Object.values(this._hass?.states || {}).filter(s => s.attributes?.plant_care === true && (!this._config.entities || this._config.entities.includes(s.entity_id))).sort((a,b) => (a.attributes.plant_name || '').localeCompare(b.attributes.plant_name || '', 'ru'));
    }
    _navigate() { history.pushState(null,'','/config/integrations/integration/adaptive_plant'); window.dispatchEvent(new Event('location-changed')); }
    _loadHistory() {
      if (!this._hass?.callApi) return;
      for (const plant of this._plants()) {
        const source = plant.attributes.source_entity;
        if (!source) continue;
        const key = `${source}|${this._days}`;
        const old = this._history[key];
        if (old && (old.loading || Date.now() - old.fetchedAt < 300000)) continue;
        const end = new Date(); const start = new Date(end.getTime() - this._days * 86400000);
        const entry = this._history[key] = {loading:true, start:start.getTime(), end:end.getTime(), fetchedAt:Date.now()};
        const path = `history/period/${encodeURIComponent(start.toISOString())}?filter_entity_id=${encodeURIComponent(source)}&end_time=${encodeURIComponent(end.toISOString())}&minimal_response`;
        this._hass.callApi('GET', path).then(result => {
          if (this._history[key] !== entry) return;
          entry.points = Array.isArray(result?.[0]) ? result[0] : [];
          entry.loading = false; this._render();
        }).catch(() => { if (this._history[key] !== entry) return; entry.loading = false; entry.error = true; this._render(); });
      }
    }
    _graph(a) {
      if (!a.source_entity) return '<div class="graph-message">Нет датчика для истории</div>';
      const h = this._history[`${a.source_entity}|${this._days}`];
      if (!h || h.loading) return '<div class="graph-message">Загрузка истории…</div>';
      if (h.error) return '<div class="graph-message">Не удалось загрузить историю <button data-action="retry">Повторить</button></div>';
      const threshold = number(a.dry_threshold);
      let previous = null; let shapes = ''; let count = 0;
      for (const item of h.points) {
        const t = Date.parse(item.last_changed || item.last_updated); const value = number(item.state);
        if (!Number.isFinite(t)) { previous = null; continue; }
        const x = Math.max(0,Math.min(1000,(t-h.start)/(h.end-h.start)*1000));
        if (previous && t >= previous.t) shapes += `<line class="history-line" x1="${previous.x}" y1="${previous.y}" x2="${x}" y2="${previous.y}"/>`;
        if (value === null || value < 0 || value > 100) { previous = null; continue; }
        const y = 100 - value;
        if (previous && t >= previous.t) shapes += `<line class="history-line" x1="${x}" y1="${previous.y}" x2="${x}" y2="${y}"/>`;
        shapes += `<circle class="history-point" cx="${x}" cy="${y}" r="1.6"/>`; count++; previous = {t,x,y};
      }
      if (previous) shapes += `<line class="history-line" x1="${previous.x}" y1="${previous.y}" x2="1000" y2="${previous.y}"/>`;
      if (!count) return '<div class="graph-message">Нет записей влажности за этот период</div>';
      const line = threshold !== null && threshold >= 0 && threshold <= 100 ? `<line class="threshold" x1="0" x2="1000" y1="${100-threshold}" y2="${100-threshold}"/>` : '';
      return `<svg class="chart" viewBox="0 0 1000 100" preserveAspectRatio="none" role="img" aria-label="История относительной влажности">${line}${shapes}</svg><div class="axis"><span>${this._days} дн. до конца периода</span><span>до ${esc(dateLabel(new Date(h.end).toISOString()))}</span></div><div class="chart-note">0–100 %${line ? ` · Порог сухости ${esc(threshold)} %` : ''} · ступени — состояния Recorder, разрывы — неизвестно</div>`;
    }
    _plant(state, preview) {
      const a = state.attributes; const status = words[preview || state.state] ? preview || state.state : 'sensor_error';
      const fresh = this._hass.states[a.freshness_entity];
      const stamp = a.last_sample_at ?? fresh?.attributes?.sampled_at ?? fresh?.last_reported ?? fresh?.last_updated;
      const moisture = number(a.moisture); const picture = imageURL(a.picture, a.picture_revision);
      const last = dateLabel(a.last_watering_at);
      const wateringTime = typeof a.last_watering_at === 'string' && a.last_watering_at.includes('T') ? Date.parse(a.last_watering_at) : NaN;
      const justWatered = status === 'ok' && ['auto','user_report','upstream'].includes(a.last_watering_source) && Number.isFinite(wateringTime) && Date.now() >= wateringTime && Date.now() - wateringTime <= 30 * 60000;
      const statusLabel = justWatered ? `Только что полито${a.last_watering_source === 'auto' ? ' · оценка' : ''}` : words[status];
      const source = {auto:'автоматически, оценка',user_report:'сообщено пользователем',upstream:'данные Adaptive Plant',unknown:'источник неизвестен'}[a.last_watering_source] || 'источник неизвестен';
      const forecast = a.forecast_status === 'available' && dateLabel(a.forecast_due_at) ? `Оценка: ${dateLabel(a.forecast_due_at)}` : forecastWords[a.forecast_status] || 'Недостаточно данных для оценки';
      const voice = dateLabel(a.voice_next_at) ? `Алиса: ${dateLabel(a.voice_next_at)}` : `Алиса: ${a.voice_unavailable_reason || 'время пока неизвестно'}`;
      const wet = {unknown:'Высокая влажность: недостаточно данных',normal:'Длительной высокой влажности не обнаружено',checking:'Проверяется длительная высокая влажность',prolonged:`Влажность длительно высокая${dateLabel(a.wet_since) ? ` с ${dateLabel(a.wet_since)}` : ''}`}[a.wet_risk] || 'Высокая влажность: недостаточно данных';
      let notice = status === 'sensor_error' ? 'Это не просьба поливать: сначала проверь датчик.' : status === 'checking' ? `Проверяю, сохранится ли сухость ${esc(a.confirmation_hours || 6)} ч.` : status === 'overdue' ? 'Полив просрочен больше суток.' : '';
      const snoozed = dateLabel(a.snoozed_until);
      return `<section class="plant ${status}"><div class="body"><button class="photo-button" data-action="photo" data-entry="${esc(a.entry_id)}" ${preview || !a.entry_id ? 'disabled' : ''} aria-label="${picture ? 'Изменить' : 'Добавить'} фото ${esc(a.plant_name)}">${picture ? `<img class="photo" src="${esc(picture)}" alt="" loading="lazy">` : '<span class="photo placeholder"><ha-icon icon="mdi:sprout"></ha-icon><span>Добавить фото</span></span>'}</button><div><h3 class="name">${esc(a.plant_name)}</h3><div class="status">${esc(statusLabel)}</div><div class="moisture">${moisture === null ? '—' : `${esc(Math.round(moisture))}<small> %</small>`}</div><div class="caption">относительная влажность · ${esc(ageLabel(stamp))}${status === 'sensor_error' ? ' · данные устарели' : ''}</div></div></div>
        ${notice ? `<div class="notice">${notice}</div>` : ''}<div class="graph-head"><span>История влажности</span><span>${[1,7,30].map(d => `<button data-action="range" data-days="${d}" aria-pressed="${d===this._days}">${d} дн.</button>`).join('')}</span></div><div class="graph">${this._graph(a)}</div>
        <div class="facts"><div>Полив: ${last ? `${esc(last)} · ${esc(source)}` : 'нет данных'}</div><div>Следующий полив · ${esc(forecast)}</div><div>${esc(voice)}</div><div>${esc(wet)}</div>${snoozed ? `<div>Пауза напоминаний до ${esc(snoozed)}</div>` : ''}</div>
        <div class="actions"><button data-action="snooze" data-entity="${esc(state.entity_id)}" ${preview ? 'disabled' : ''}>Пауза напоминаний на 24 часа</button><button data-action="options">Настройки</button></div></section>`;
    }
    _render() {
      if (!this._hass) return;
      const plants = this._plants(); const preview = this._config.preview_status; const voice = this._hass.states['input_boolean.plant_care_voice_enabled']?.state === 'on';
      this.shadowRoot.innerHTML = `<style>
        :host{display:block;font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif);color:var(--primary-text-color,#243029)}*{box-sizing:border-box}ha-card{background:var(--ha-card-background,var(--card-background-color,#fff));box-shadow:none;border:1px solid var(--divider-color,#ddd);border-radius:14px;overflow:hidden}.header,.footer{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 16px}.title{font-size:20px;font-weight:650}.plants{padding:0 16px}.plant{border-top:1px solid var(--divider-color,#ddd);padding:14px 0}.body{display:grid;grid-template-columns:72px minmax(0,1fr);gap:12px;align-items:start}.photo-button{padding:0;border:0;background:none;min-width:72px}.photo{display:block;width:72px;height:82px;object-fit:cover;border-radius:8px}.placeholder{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:5px;background:var(--secondary-background-color,#f1f3ef);color:var(--secondary-text-color,#667);font-size:10px}.name{margin:0;font-size:19px}.status{font-size:12px;color:var(--secondary-text-color,#667);margin-top:3px}.due .status,.overdue .status{color:var(--error-color,#a63c27)}.moisture{font-size:32px;font-weight:650;line-height:1;margin-top:5px}.moisture small{font-size:17px;font-weight:400}.caption,.chart-note,.axis{color:var(--secondary-text-color,#667);font-size:11px}.caption{margin-top:3px}.graph-head{display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-top:12px;font-weight:600}.graph-head button{font-size:11px;min-height:36px;padding:5px 7px}.graph-head button[aria-pressed=true]{color:var(--primary-color,#39785b);font-weight:700}.graph{margin-top:4px}.chart{width:100%;height:90px;overflow:visible}.history-line{stroke:var(--primary-color,#39785b);stroke-width:2;vector-effect:non-scaling-stroke}.history-point{fill:var(--primary-color,#39785b)}.threshold{stroke:var(--secondary-text-color,#888);stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}.axis{display:flex;justify-content:space-between}.chart-note{margin-top:3px}.graph-message{color:var(--secondary-text-color,#667);font-size:12px;padding:18px 0}.facts{font-size:12px;line-height:1.55;margin-top:10px}.notice{font-size:12px;margin-top:8px;color:var(--error-color,#a63c27)}button{font:inherit;cursor:pointer;color:inherit;border:0;background:transparent;border-radius:6px;min-height:40px;padding:7px 9px}button:focus-visible{outline:2px solid var(--primary-color,#39785b)}button:disabled{opacity:.5;cursor:default}.actions{display:flex;flex-wrap:wrap;gap:4px;margin:7px -9px 0}.actions button,.footer button,.header button{font-size:12px;color:var(--primary-color,#39785b)}.footer{border-top:1px solid var(--divider-color,#ddd);font-size:11px;color:var(--secondary-text-color,#667)}.preview,.empty{padding:10px 16px;font-size:12px}@media(max-width:420px){.plants{padding:0 12px}.header,.footer{padding:10px 12px}.facts{font-size:12px}}
      </style><ha-card><div class="header"><div class="title">Растения</div><button data-action="add">Добавить</button></div>${preview ? '<div class="preview">ПРЕДПРОСМОТР · действия отключены</div>' : ''}<div class="plants">${plants.length ? plants.map(p => this._plant(p,preview)).join('') : '<div class="empty">Добавь первое растение через Adaptive Plant.</div>'}</div><div class="footer"><span>Голос после суток просрочки · 08:00–21:00</span><button data-action="voice" ${preview ? 'disabled' : ''}>Алиса: ${voice ? 'вкл.' : 'выкл.'}</button></div></ha-card>`;
      this.shadowRoot.querySelectorAll('[data-action]').forEach(button => {
        if (this._pending.has(`${button.dataset.action}:${button.dataset.entity || button.dataset.entry || ''}`)) button.disabled = true;
        button.addEventListener('click', () => this._action(button));
      });
    }
    async _action(button) {
      const action = button.dataset.action;
      if (action === 'add' || action === 'options') return this._navigate();
      if (action === 'photo') {
        if (this._config.preview_status || !button.dataset.entry || this._pending.has(`photo:${button.dataset.entry}`)) return;
        const input = document.createElement('input');
        input.type = 'file'; input.accept = 'image/*';
        input.addEventListener('change', () => { if (input.files?.[0]) this._uploadPhoto(button.dataset.entry, input.files[0]); });
        input.click(); return;
      }
      if (action === 'range') { this._days = Number(button.dataset.days); this._loadHistory(); this._render(); return; }
      if (action === 'retry') { this._history = {}; this._loadHistory(); this._render(); return; }
      if (this._config.preview_status) return;
      const key = `${action}:${button.dataset.entity || ''}`;
      if (this._pending.has(key)) return;
      this._pending.add(key); this._render();
      try {
        if (action === 'voice') await this._hass.callService('input_boolean','toggle',{entity_id:'input_boolean.plant_care_voice_enabled'});
        if (action === 'snooze') await this._hass.callService('plant_care_alerts','snooze',{entity_id:button.dataset.entity});
      } catch (_) { this.dispatchEvent(new CustomEvent('hass-notification',{bubbles:true,composed:true,detail:{message:'Действие не выполнено. Проверь соединение с Home Assistant.'}})); }
      finally { this._pending.delete(key); this._render(); }
    }
    async _uploadPhoto(entryId, file) {
      if (!file.type.startsWith('image/')) {
        this._notify('Выбери файл изображения.');
        return;
      }
      const key = `photo:${entryId}`;
      if (this._pending.has(key)) return;
      this._pending.add(key); this._render();
      let fileId;
      try {
        const form = new FormData(); form.append('file', file);
        const response = await this._hass.fetchWithAuth('/api/file_upload', {method:'POST',body:form});
        if (!response.ok) throw new Error('upload failed');
        fileId = (await response.json()).file_id;
        if (!fileId) throw new Error('missing file id');
        await this._hass.callService('plant_care_alerts','set_photo',{entry_id:entryId,file_id:fileId});
      } catch (_) {
        if (fileId) {
          try { await this._hass.callApi('DELETE','file_upload',{file_id:fileId}); } catch (_) { /* May already be consumed by the service. */ }
        }
        this._notify('Не удалось сохранить фото. Проверь формат изображения и права администратора.');
      } finally { this._pending.delete(key); this._render(); }
    }
    _notify(message) { this.dispatchEvent(new CustomEvent('hass-notification',{bubbles:true,composed:true,detail:{message}})); }
  }
  if (!customElements.get('plant-care-card')) customElements.define('plant-care-card',PlantCareCard);
  window.customCards = window.customCards || [];
  if (!window.customCards.some(c => c.type === 'plant-care-card')) window.customCards.push({type:'plant-care-card',name:'Растения — полив',description:'Влажность, история и оценка полива.',preview:true});
})();
