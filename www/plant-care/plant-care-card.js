// SPDX-License-Identifier: MIT
// Companion view: Adaptive Plant owns its settings; Recorder owns the measurements.
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const words = {ok:'Показания влажности',calibrating:'Проверяю шкалу датчика',checking:'Проверяю сухость',due:'Пора проверить грунт',overdue:'Полив просрочен',snoozed:'Напоминания на паузе',sensor_error:'Недостоверные показания'};
  const reliabilityWords = {starting:'Запуск датчика: ожидаю первое измерение.',lower_saturation:'Индекс остаётся на нижней границе 0% более 12 часов. Степень сухости по нему не определить.',history_unavailable:'История измерений пока недоступна. Проверяю достоверность показаний.',saturated:'Индекс остаётся на 100% более 12 часов: по нему нельзя оценить влажность грунта.',unchanged:'Показания не меняются более 12 часов.',raw_unchanged:'Исходные измерения датчика не меняются более 12 часов.',stale:'Датчик не передаёт свежих измерений.',invalid_sample:'Датчик передал недопустимые данные или время измерения.',recovering:'Получены новые измерения. Проверяю устойчивое восстановление.'};
  const forecastWords = {invalid_readings:'Недостоверные показания: прогноз отключён',insufficient_data:'Недостаточно данных для оценки',saturated:'Индекс насыщен: оценить высыхание нельзя',unstable:'Показания нестабильны: оценка недоступна',stale:'Нет свежих данных для оценки',no_drying_trend:'Устойчивого высыхания пока не видно'};
  const calibrationWords = {
    awaiting_reference:'наблюдение — нужен независимый эталон',
    awaiting_source:'наблюдение — ожидаю данные датчика и настройки',
    witness_unavailable:'приостановлена — контрольная история недоступна',
    concurrent_change:'наблюдение — настройка изменилась во время проверки',
    effect_unconfirmed:'наблюдение — результат записи пока не подтверждён',
    context_unavailable:'приостановлена — условия записи не подтверждены',
    unsupported_runtime:'приостановлена — способ записи ещё не проверен',
    service_conflict:'приостановлена — запись настройки сейчас недоступна',
    evidence_limit:'наблюдение — слишком много данных для проверки',
    awaiting_cycles:'наблюдение — собираю подтверждённые опорные циклы',
    insufficient_span:'наблюдение — истории опорных циклов пока мало',
    awaiting_holdout:'наблюдение — ожидаю отдельное контрольное окно',
    cooldown:'наблюдение — перерыв между изменениями шкалы',
    deadband:'наблюдение — подтверждённое отклонение слишком мало',
    helper_resolution:'наблюдение — изменение меньше выбранного шага записи',
    applying:'сохраняю изменение шкалы',
    applied:'изменение шкалы сохранено',
    rolled_back:'предыдущая шкала восстановлена',
    storage_unavailable:'наблюдение — журнал временно недоступен',
    source_changed:'наблюдение — источник измерений изменился',
    dry_changed:'наблюдение — сухая опорная точка изменена',
    manual_override:'наблюдение — шкала изменена извне',
    outside_baseline_envelope:'наблюдение — изменение выходит за допустимый предел',
    target_out_of_bounds:'наблюдение — изменение выходит за допустимый предел',
    unreliable_sample:'наблюдение — показания пока недостоверны',
    stale_sample:'наблюдение — ожидаю свежие измерения',
    poor_reference_quality:'наблюдение — опорные измерения недостаточно устойчивы',
    holdout_failed:'наблюдение — контрольное окно не подтвердило улучшение',
    invalid_reference:'наблюдение — независимый эталон не подтверждён',
  };
  const calibrationEventWords = {
    automatic:'Автоматическая калибровка',
    rollback:'Откат к предыдущей шкале',
    manual:'Изменение настройки',
    external_discovered:'Обнаружено внешнее изменение',
  };
  const calibrationReasonTitles = {
    concurrent_change:'Автоматическая запись при конфликте настроек',
  };
  const calibrationReasonWords = {
    qualified_drift:'Независимые опорные измерения подтвердили улучшение.',
    contradiction:'На последующем независимом измерении прежняя шкала дала меньшую ошибку.',
    pending_effect_reconciled:'Проверено по текущим настройкам; время и автор изменения неизвестны.',
    concurrent_change:'Во время записи настройка была изменена извне; калибровка не подтверждена.',
  };
  const adcFormatter = new Intl.NumberFormat('ru-RU', {maximumFractionDigits:6});
  const adcLabel = value => Number.isFinite(value) ? adcFormatter.format(value) : '—';
  const validEpoch = value => Number.isFinite(value) && value > 0
    && Number.isFinite(new Date(value * 1000).getTime());
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
    constructor() {
      super(); this.attachShadow({mode:'open'});
      this._config = {}; this._history = {}; this._days = 7;
      this._pending = new Set(); this._calibrationOpen = new Set(); this._wateringOpen = new Set();
      this._ranges = new Map(); this._selectedPoints = new Map(); this._selectedEntity = null;
    }
    setConfig(config) { this._config = config || {}; this._render(); }
    static getStubConfig() { return {type:'custom:plant-care-card'}; }
    getCardSize() { return 2; }
    getGridOptions() { return {columns:6,min_columns:3,rows:'auto'}; }
    set hass(hass) { this._hass = hass; this._loadHistory(); this._render(); }
    get hass() { return this._hass; }
    _plants() {
      return Object.values(this._hass?.states || {}).filter(s => s.attributes?.plant_care === true && (!this._config.entities || this._config.entities.includes(s.entity_id))).sort((a,b) => (a.attributes.plant_name || '').localeCompare(b.attributes.plant_name || '', 'ru'));
    }
    _navigate(domain = 'adaptive_plant') { history.pushState(null,'',`/config/integrations/integration/${domain}`); window.dispatchEvent(new Event('location-changed')); }
    _loadHistory() {
      if (!this._selectedEntity || !this._hass?.callApi) return;
      const plant = this._plants().find(item => item.entity_id === this._selectedEntity);
      const source = plant?.attributes.source_entity;
      if (!source) return;
      const key = `${source}|${this._days}`;
      const old = this._history[key];
      if (old && (old.loading || Date.now() - old.fetchedAt < 300000)) return;
      const end = new Date(); const start = new Date(end.getTime() - this._days * 86400000);
      const entry = this._history[key] = {
        ...old, loading:true, error:false, fetchedAt:Date.now(),
        start:old?.start ?? start.getTime(), end:old?.end ?? end.getTime(),
      };
      const path = `history/period/${encodeURIComponent(start.toISOString())}?filter_entity_id=${encodeURIComponent(source)}&end_time=${encodeURIComponent(end.toISOString())}&minimal_response`;
      this._hass.callApi('GET', path).then(result => {
        if (this._history[key] !== entry) return;
        Object.assign(entry, {points:Array.isArray(result?.[0]) ? result[0] : [], start:start.getTime(), end:end.getTime(), loading:false});
        this._render();
      }).catch(() => {
        if (this._history[key] !== entry) return;
        entry.loading = false; entry.error = true; this._render();
      });
    }
    _historyShapes(h) {
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
      return {shapes, count};
    }
    _historyPoints(h) {
      return (h?.points || []).map(item => ({
        time:Date.parse(item.last_changed || item.last_updated), value:number(item.state),
      })).filter(point => Number.isFinite(point.time) && point.value !== null && point.value >= 0 && point.value <= 100)
        .sort((a,b) => a.time - b.time);
    }
    _nearestHistoryPoint(a, fraction) {
      const h = this._history[`${a.source_entity}|${this._days}`];
      const points = this._historyPoints(h);
      if (!points.length || !Number.isFinite(fraction)) return null;
      const target = h.start + Math.max(0,Math.min(1,fraction)) * (h.end-h.start);
      return points.reduce((nearest,point) => Math.abs(point.time-target) < Math.abs(nearest.time-target) ? point : nearest);
    }
    _pointKey(a, entityId) { return `${entityId || a.source_entity}|${this._days}`; }
    _selectedPoint(a, entityId, points) {
      const chosen = this._selectedPoints.get(this._pointKey(a,entityId));
      if (chosen === undefined) return points.at(-1);
      return points.find(point => point.time === chosen)
        || points.reduce((nearest,point) => Math.abs(point.time-chosen) < Math.abs(nearest.time-chosen) ? point : nearest);
    }
    _graph(a, entityId) {
      if (!a.source_entity) return '<div class="graph-message">Нет датчика для истории</div>';
      const h = this._history[`${a.source_entity}|${this._days}`];
      if (!h || (h.loading && !h.points)) return '<div class="graph-message">Загрузка истории…</div>';
      if (h.error && !h.points) return '<div class="graph-message">Не удалось загрузить историю <button data-action="retry">Повторить</button></div>';
      const threshold = number(a.dry_threshold);
      const {shapes,count} = this._historyShapes(h);
      const points = this._historyPoints(h);
      if (!count || !points.length) return '<div class="graph-message">Нет записей влажности за этот период</div>';
      const point = this._selectedPoint(a,entityId,points);
      const x = Math.max(0,Math.min(1000,(point.time-h.start)/(h.end-h.start)*1000));
      const index = points.indexOf(point);
      const date = new Date(point.time).toISOString();
      const value = adcLabel(point.value);
      const line = threshold !== null && threshold >= 0 && threshold <= 100 ? `<line class="threshold" x1="0" x2="1000" y1="${100-threshold}" y2="${100-threshold}"/>` : '';
      return `<svg class="chart" data-key="chart" viewBox="0 0 1000 100" preserveAspectRatio="none" tabindex="0" role="slider" aria-label="Выбор измерения влажности" aria-valuemin="0" aria-valuemax="${points.length-1}" aria-valuenow="${index}" aria-valuetext="${esc(dateLabel(date))}, ${esc(value)} процентов">${line}${shapes}<line class="inspection-line" x1="${x}" x2="${x}" y1="0" y2="100"/><circle class="inspection-point" cx="${x}" cy="${100-point.value}" r="3"/></svg><output class="graph-inspection" data-key="inspection" aria-live="polite" data-value="${point.value}" data-timestamp="${point.time}"><time datetime="${date}">${esc(dateLabel(date))}</time><strong data-reading-value>${esc(value)} %</strong></output><div class="axis" data-key="axis"><span>${this._days} дн. до конца периода</span><span>до ${esc(dateLabel(new Date(h.end).toISOString()))}</span></div><div class="chart-note" data-key="note">0–100 %${line ? ` · Порог сухости ${esc(threshold)} %` : ''} · выбери точку на графике или стрелками</div><div class="chart-note" data-key="quality">Ступени — состояния Recorder, разрывы — неизвестно${h.error ? ' · Обновление не удалось; показана ранее загруженная история' : ''}</div>`;
    }
    _inspectPointer(event) {
      const chart = event.target.closest?.('.chart');
      if (!chart || !this._selectedEntity) return;
      const state = this._hass.states[this._selectedEntity];
      const rect = chart.getBoundingClientRect();
      if (!state || !rect.width) return;
      const point = this._nearestHistoryPoint(state.attributes,(event.clientX-rect.left)/rect.width);
      if (!point) return;
      this._selectedPoints.set(this._pointKey(state.attributes,state.entity_id),point.time);
      this._render();
    }
    _inspectKeyboard(event) {
      if (!event.target.matches?.('.chart') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      const state = this._hass.states[this._selectedEntity];
      if (!state) return;
      const points = this._historyPoints(this._history[`${state.attributes.source_entity}|${this._days}`]);
      if (!points.length) return;
      event.preventDefault();
      const current = points.indexOf(this._selectedPoint(state.attributes,state.entity_id,points));
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? points.length-1
        : Math.max(0,Math.min(points.length-1,current + (event.key === 'ArrowLeft' ? -1 : 1)));
      this._selectedPoints.set(this._pointKey(state.attributes,state.entity_id),points[index].time);
      this._render();
    }
    _statusLabel(a, status) {
      const wateringTime = typeof a.last_watering_at === 'string' && a.last_watering_at.includes('T')
        ? Date.parse(a.last_watering_at) : NaN;
      const justWatered = status === 'ok'
        && ['auto','user_report','upstream'].includes(a.last_watering_source)
        && Number.isFinite(wateringTime) && Date.now() >= wateringTime
        && Date.now() - wateringTime <= 30 * 60000;
      return justWatered
        ? `Только что полито${a.last_watering_source === 'auto' ? ' · оценка' : ''}`
        : words[status];
    }
    _plantPhoto(a, preview) {
      const picture = imageURL(a.picture, a.picture_revision);
      return `<button class="photo-button" data-action="photo" data-entry="${esc(a.entry_id)}" ${preview || !a.entry_id ? 'disabled' : ''} aria-label="${picture ? 'Изменить' : 'Добавить'} фото ${esc(a.plant_name)}">${picture ? `<img class="photo" src="${esc(picture)}" alt="" loading="lazy">` : '<span class="photo placeholder"><ha-icon icon="mdi:sprout"></ha-icon><span>Добавить фото</span></span>'}</button>`;
    }
    _plantReading(a, status) {
      const fresh = this._hass.states[a.freshness_entity];
      const stamp = a.last_sample_at ?? fresh?.attributes?.sampled_at;
      const moisture = this._trustedMoisture(a,status);
      const changed = a.last_value_change_at
        ? `<br>Значение не менялось с ${esc(dateLabel(a.last_value_change_at))}` : '';
      return `<div><h3 class="name">${esc(a.plant_name)}</h3><div class="status">${esc(this._statusLabel(a, status))}</div><div class="moisture">${moisture === null ? '—' : `${esc(Math.round(moisture))}<small> %</small>`}</div><div class="caption">Относительный индекс · замер: ${esc(ageLabel(stamp))}${changed}</div></div>`;
    }
    _plantNotice(a, status) {
      if (status === 'calibrating') return 'Проверяю новые измерения после изменения шкалы.';
      if (status === 'sensor_error') {
        return esc(reliabilityWords[a.reliability_reason] || 'Показания датчика недостоверны.')
          + ' Полив по этим данным не определяется. Проверь грунт вручную.';
      }
      if (status === 'checking') return `Проверяю, сохранится ли сухость ${esc(a.confirmation_hours || 6)} ч.`;
      if (status === 'overdue') return 'Полив просрочен больше суток.';
      return '';
    }
    _wateringLabel(a, compact = false) {
      const value = a.last_watering_at;
      if (!value) return 'нет данных';
      const dayOnly = /^\d{4}-\d{2}-\d{2}$/.test(String(value));
      let label = dateLabel(value);
      if (!label) return 'нет данных';
      if (compact) {
        const stamp = new Date(dayOnly ? `${value}T12:00:00` : value);
        label = new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short'}).format(stamp);
      }
      if (a.last_watering_precision === 'approximate_day') return `${compact ? '≈ ' : 'примерно '}${label}`;
      if (a.last_watering_precision === 'interval' && !compact) {
        const start = dateLabel(a.last_watering_window_start);
        const end = dateLabel(a.last_watering_window_end);
        if (start && end) return `${start} — ${end}`;
      }
      return label;
    }
    _wateringFact(a) {
      const source = {auto:'автоматически, оценка',user_report:'сообщено пользователем',upstream:'данные Adaptive Plant',unknown:'источник неизвестен'}[a.last_watering_source] || 'источник неизвестен';
      const note = typeof a.last_watering_note === 'string' && a.last_watering_note ? ` · ${esc(a.last_watering_note)}` : '';
      return `<div>Полив: ${esc(this._wateringLabel(a))}${a.last_watering_at ? ` · ${esc(source)}${note}` : ''}</div>`;
    }
    _wateringHistory(a, entityId) {
      if (!Array.isArray(a.watering_history)) return '';
      const events = a.watering_history.filter(event => event && typeof event.event_id === 'string'
        && typeof event.valid === 'boolean' && dateLabel(event.at));
      if (!events.length) return '';
      const rows = [...events].sort((first,second) => Date.parse(second.at)-Date.parse(first.at)).map(event => {
        const attributes = {
          last_watering_at:event.at,last_watering_precision:event.precision,
          last_watering_window_start:event.window_start,last_watering_window_end:event.window_end,
        };
        const title = event.valid ? {auto:'Автоматически, оценка',user_report:'Сообщено пользователем',upstream:'Данные Adaptive Plant'}[event.source] || 'Источник неизвестен' : 'Оценка отозвана';
        const detected = event.retroactive && dateLabel(event.detected_at)
          ? `<div>Обнаружено позднее: ${esc(dateLabel(event.detected_at))}</div>` : '';
        const retracted = !event.valid && dateLabel(event.retracted_at)
          ? `<div>Отозвано: ${esc(dateLabel(event.retracted_at))}</div>` : '';
        return `<li class="watering-event${event.valid ? '' : ' retracted'}" data-key="${esc(event.event_id)}"><div>${esc(title)} · ${esc(this._wateringLabel(attributes))}</div>${typeof event.note === 'string' && event.note ? `<div>${esc(event.note)}</div>` : ''}${detected}${retracted}</li>`;
      }).join('');
      return `<details class="watering-details" data-entity="${esc(entityId)}"${this._wateringOpen.has(entityId) ? ' open' : ''}><summary>История полива</summary><ol class="watering-history">${rows}</ol></details>`;
    }
    _trustedMoisture(a,status) {
      const value = number(a.moisture);
      return ['sensor_error','calibrating'].includes(status) || a.reliability_valid === false
        || value === null || value < 0 || value > 100 ? null : value;
    }
    _tile(state, preview) {
      const a = state.attributes;
      const status = words[preview || state.state] ? preview || state.state : 'sensor_error';
      const moisture = this._trustedMoisture(a,status);
      const picture = imageURL(a.picture,a.picture_revision);
      const source = a.last_watering_source === 'auto' ? ' · оценка' : '';
      return `<button class="plant-tile ${status}" data-key="${esc(state.entity_id)}" data-action="open" data-entity="${esc(state.entity_id)}" aria-haspopup="dialog"><span class="tile-photo">${picture ? `<img class="photo" src="${esc(picture)}" alt="" loading="lazy">` : '<ha-icon icon="mdi:sprout" aria-hidden="true"></ha-icon>'}</span><span class="tile-copy"><span class="tile-name">${esc(a.plant_name || state.entity_id)}</span><span class="tile-moisture">${moisture === null ? '—' : `${esc(Math.round(moisture))}<small> %</small>`}</span><span class="tile-status">${esc(this._statusLabel(a,status))}</span><span class="tile-watering">Полив: ${esc(this._wateringLabel(a,true))}${source}</span></span></button>`;
    }
    _forecastLabel(a) {
      const date = dateLabel(a.forecast_due_at);
      return a.forecast_status === 'available' && date
        ? `Оценка: ${date}`
        : forecastWords[a.forecast_status] || 'Недостаточно данных для оценки';
    }
    _voiceLabel(a) {
      const date = dateLabel(a.voice_next_at);
      return date ? `Алиса: ${date}`
        : `Алиса: ${a.voice_unavailable_reason || 'время пока неизвестно'}`;
    }
    _wetLabel(a) {
      const since = dateLabel(a.wet_since);
      return {
        unknown:'Высокая влажность: недостаточно данных',
        normal:'Длительной высокой влажности не обнаружено',
        checking:'Проверяется длительная высокая влажность',
        prolonged:`Влажность длительно высокая${since ? ` с ${since}` : ''}`
      }[a.wet_risk] || 'Высокая влажность: недостаточно данных';
    }
    _calibrationEvents(a) {
      if (a.calibration_history_available !== true || !Array.isArray(a.calibration_history)) return null;
      return a.calibration_history.filter(event => event
        && typeof event.event_id === 'string' && event.event_id
        && Object.hasOwn(calibrationEventWords, event.kind)
        && [event.old_dry,event.old_wet,event.new_dry,event.new_wet].every(Number.isFinite)
        && Number.isInteger(event.revision) && event.revision > 0
        && (event.at === null ? validEpoch(event.detected_at) : validEpoch(event.at)))
        .sort((first,second) => second.revision - first.revision);
    }
    _calibrationEvent(event) {
      const discovered = event.at === null;
      const stamp = discovered ? event.detected_at : event.at;
      const title = Object.hasOwn(calibrationReasonTitles, event.reason)
        ? calibrationReasonTitles[event.reason] : calibrationEventWords[event.kind];
      const reason = calibrationReasonWords[event.reason];
      const before = `${adcLabel(event.old_dry)} / ${adcLabel(event.old_wet)}`;
      const after = `${adcLabel(event.new_dry)} / ${adcLabel(event.new_wet)}`;
      return `<li class="calibration-event"><div>${esc(title)} · <time data-time-kind="${discovered ? 'detected' : 'changed'}" datetime="${esc(new Date(stamp * 1000).toISOString())}">${esc(dateLabel(stamp))}</time></div><div class="calibration-values">${esc(before)} → ${esc(after)} ADC</div>${reason ? `<div class="calibration-reason">${esc(reason)}</div>` : ''}${discovered ? '<div class="calibration-note">Время изменения неизвестно</div>' : ''}</li>`;
    }
    _calibration(a, entityId) {
      if (typeof a.calibration_enabled !== 'boolean') return '';
      const label = a.calibration_enabled
        ? calibrationWords[a.calibration_status] || 'наблюдение' : 'отключена';
      const events = this._calibrationEvents(a);
      const current = `${adcLabel(a.calibration_dry_adc)} / ${adcLabel(a.calibration_wet_adc)}`;
      const baseline = `${adcLabel(a.calibration_baseline_dry)} / ${adcLabel(a.calibration_baseline_wet)}`;
      const journal = events === null
        ? '<div class="calibration-unavailable">История калибровки временно недоступна</div>'
        : events.length
          ? `<ol class="calibration-history">${events.map(event => this._calibrationEvent(event)).join('')}</ol>`
          : '<div class="calibration-empty">Записанных изменений калибровки пока нет</div>';
      return `<div class="calibration" role="group" aria-label="Автокалибровка"><div class="calibration-status">Автокалибровка: ${esc(label)}</div><details class="calibration-details" data-entity="${esc(entityId)}"${this._calibrationOpen.has(entityId) ? ' open' : ''}><summary>Шкала и история изменений</summary><div class="calibration-scale">Шкала, сухая / влажная: ${esc(current)} ADC<br>Исходная шкала: ${esc(baseline)} ADC</div>${journal}</details></div>`;
    }
    _plantFacts(a, status) {
      const snoozed = dateLabel(a.snoozed_until);
      const priorDry = status === 'sensor_error' && a.due_since
        ? `<div>До ошибки сухость была подтверждена ${esc(dateLabel(a.due_since))}.</div>` : '';
      return `<div class="facts">${priorDry}${this._wateringFact(a)}<div>Следующий полив · ${esc(this._forecastLabel(a))}</div><div>${esc(this._voiceLabel(a))}</div><div>${esc(this._wetLabel(a))}</div>${snoozed ? `<div>Пауза напоминаний до ${esc(snoozed)}</div>` : ''}</div>`;
    }
    _plant(state, preview) {
      const a = state.attributes;
      const status = words[preview || state.state] ? preview || state.state : 'sensor_error';
      const notice = this._plantNotice(a,status);
      return `<section class="plant ${status}" data-key="${esc(state.entity_id)}" data-entity="${esc(state.entity_id)}"><div class="body" data-key="reading"><div data-key="photo">${this._plantPhoto(a,preview)}</div><div data-key="value">${this._plantReading(a,status)}</div></div><div class="notice" data-key="notice">${notice}</div><div class="graph-head" data-key="graph-heading"><span>История влажности</span><span>${[1,7,30].map(d => `<button data-action="range" data-days="${d}" aria-pressed="${d===this._days}">${d} дн.</button>`).join('')}</span></div><div class="graph" data-key="graph">${this._graph(a,state.entity_id)}</div><div data-key="facts">${this._plantFacts(a,status)}</div><div data-key="watering">${this._wateringHistory(a,state.entity_id)}</div><div data-key="calibration">${this._calibration(a,state.entity_id)}</div><div class="actions" data-key="actions"><button data-action="snooze" data-entity="${esc(state.entity_id)}" ${preview ? 'disabled' : ''}>Пауза напоминаний на 24 часа</button><button data-action="options">Настройки</button></div></section>`;
    }
    _rememberCalibrationSections() {
      for (const [selector,opened] of [['.calibration-details[data-entity]',this._calibrationOpen],['.watering-details[data-entity]',this._wateringOpen]]) {
        this.shadowRoot.querySelectorAll(selector).forEach(section => {
          const entityId = section.dataset.entity;
          if (!entityId) return;
          if (section.open) opened.add(entityId); else opened.delete(entityId);
        });
      }
    }
    _nodeKey(node) { return node?.nodeType === 1 ? node.getAttribute('data-key') : null; }
    _patchAttributes(current, next) {
      const preserveOpen = current.tagName === 'DETAILS';
      for (const attribute of [...current.attributes]) {
        if (preserveOpen && attribute.name === 'open') continue;
        if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
      }
      for (const attribute of [...next.attributes]) {
        if (preserveOpen && attribute.name === 'open') continue;
        if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name,attribute.value);
      }
    }
    _patchChildren(parent, desired) {
      const nodes = [...desired.childNodes];
      nodes.forEach((next,index) => {
        let current = parent.childNodes[index];
        const key = this._nodeKey(next);
        if (key && this._nodeKey(current) !== key) {
          const existing = [...parent.childNodes].slice(index).find(node => this._nodeKey(node) === key);
          if (existing) { parent.insertBefore(existing,current || null); current = existing; }
        }
        if (!current) { parent.append(next.cloneNode(true)); return; }
        if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName
          || (key && this._nodeKey(current) !== key)) {
          current.replaceWith(next.cloneNode(true)); return;
        }
        if (current.nodeType === 1) {
          this._patchAttributes(current,next); this._patchChildren(current,next);
        } else if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
      });
      while (parent.childNodes.length > nodes.length) parent.lastChild.remove();
    }
    _patchContent(container, html) {
      if (container._plantHTML === html) return;
      const desired = document.createElement('template');
      desired.innerHTML = html;
      this._patchChildren(container,desired.content);
      container._plantHTML = html;
    }
    _ensureShell() {
      if (this._shellReady) return;
      this.shadowRoot.innerHTML = '<style>' + "\n        :host{display:block;font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif);color:var(--primary-text-color,#243029)}*{box-sizing:border-box}ha-card{background:var(--ha-card-background,var(--card-background-color,#fff));box-shadow:none;border:1px solid var(--divider-color,#ddd);border-radius:14px;overflow:hidden}.header,.footer{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 16px}.title{font-size:20px;font-weight:650}.plants{padding:0 16px}.plant{border-top:1px solid var(--divider-color,#ddd);padding:14px 0}.body{display:grid;grid-template-columns:72px minmax(0,1fr);gap:12px;align-items:start}.photo-button{padding:0;border:0;background:none;min-width:72px}.photo{display:block;width:72px;height:82px;object-fit:cover;border-radius:8px}.placeholder{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:5px;background:var(--secondary-background-color,#f1f3ef);color:var(--secondary-text-color,#667);font-size:10px}.name{margin:0;font-size:19px}.status{font-size:12px;color:var(--secondary-text-color,#667);margin-top:3px}.due .status,.overdue .status,.sensor_error .status{color:var(--error-color,#a63c27)}.moisture{font-size:32px;font-weight:650;line-height:1;margin-top:5px}.moisture small{font-size:17px;font-weight:400}.caption,.chart-note,.axis{color:var(--secondary-text-color,#667);font-size:11px}.caption{margin-top:3px}.graph-head{display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-top:12px;font-weight:600}.graph-head button{font-size:11px;min-height:36px;padding:5px 7px}.graph-head button[aria-pressed=true]{color:var(--primary-color,#39785b);font-weight:700}.graph{margin-top:4px}.chart{width:100%;height:90px;overflow:visible}.history-line{stroke:var(--primary-color,#39785b);stroke-width:2;vector-effect:non-scaling-stroke}.history-point{fill:var(--primary-color,#39785b)}.threshold{stroke:var(--secondary-text-color,#888);stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}.axis{display:flex;justify-content:space-between}.chart-note{margin-top:3px}.graph-message{color:var(--secondary-text-color,#667);font-size:12px;padding:18px 0}.facts{font-size:12px;line-height:1.55;margin-top:10px}.notice{font-size:12px;margin-top:8px;color:var(--error-color,#a63c27)}button{font:inherit;cursor:pointer;color:inherit;border:0;background:transparent;border-radius:6px;min-height:40px;padding:7px 9px}button:focus-visible{outline:2px solid var(--primary-color,#39785b)}button:disabled{opacity:.5;cursor:default}.actions{display:flex;flex-wrap:wrap;gap:4px;margin:7px -9px 0}.actions button,.footer button,.header button{font-size:12px;color:var(--primary-color,#39785b)}.footer{border-top:1px solid var(--divider-color,#ddd);font-size:11px;color:var(--secondary-text-color,#667)}.preview,.empty{padding:10px 16px;font-size:12px}@media(max-width:420px){.plants{padding:0 12px}.header,.footer{padding:10px 12px}.facts{font-size:12px}}\n        .calibrating .notice{color:var(--secondary-text-color,#667)}.calibration{font-size:12px;line-height:1.5;margin-top:10px;padding-top:9px;border-top:1px solid var(--divider-color,#ddd)}.calibration-status{color:var(--secondary-text-color,#667)}.calibration-details{margin-top:3px}.calibration-details summary{cursor:pointer;min-height:32px;display:list-item;align-content:center;color:var(--primary-color,#39785b)}.calibration-details summary:focus-visible{outline:2px solid var(--primary-color,#39785b);border-radius:3px}.calibration-scale,.calibration-empty,.calibration-unavailable{margin:5px 0;color:var(--secondary-text-color,#667)}.calibration-history{list-style:none;margin:5px 0 0;padding:0}.calibration-event{padding:6px 0;border-top:1px solid var(--divider-color,#ddd)}.calibration-values,.calibration-note,.calibration-reason{color:var(--secondary-text-color,#667)}\n      \n.plants{padding:4px 12px 12px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.plant-tile{display:grid;grid-template-columns:66px minmax(0,1fr);align-items:center;gap:10px;text-align:left;width:100%;min-height:156px;padding:12px;border:1px solid var(--divider-color,#ddd);border-radius:12px;background:var(--ha-card-background,var(--card-background-color,#fff))}.tile-photo{width:66px;height:118px;display:flex;align-items:center;justify-content:center;background:transparent}.tile-photo .photo{width:66px;height:118px;object-fit:contain;background:transparent}.tile-photo ha-icon{--mdc-icon-size:42px;color:var(--secondary-text-color,#667)}.tile-copy{display:flex;flex-direction:column;min-width:0;gap:5px}.tile-name{font-size:16px;font-weight:650;overflow-wrap:anywhere}.tile-moisture{font-size:29px;font-weight:650;line-height:1.1}.tile-moisture small{font-size:15px;font-weight:400}.tile-status,.tile-watering{font-size:11px;line-height:1.35;color:var(--secondary-text-color,#667);overflow-wrap:anywhere}.sensor_error .tile-status,.due .tile-status,.overdue .tile-status{color:var(--error-color,#a63c27)}.footer{padding:5px 10px;flex-wrap:wrap}.plant-dialog{color:var(--primary-text-color,#243029);background:var(--ha-card-background,var(--card-background-color,#fff));border:1px solid var(--divider-color,#ddd);border-radius:16px;padding:0;width:min(740px,calc(100vw - 24px));max-width:calc(100vw - 24px);max-height:90dvh;overflow:auto}.plant-dialog::backdrop{background:rgba(0,0,0,.45)}.dialog-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 16px;border-bottom:1px solid var(--divider-color,#ddd);position:sticky;top:0;z-index:1;background:var(--ha-card-background,var(--card-background-color,#fff))}.dialog-header strong{font-size:14px}.dialog-header button{min-height:44px}.detail-content{padding:0 18px 18px}.detail-content .plant{border:0}.body{grid-template-columns:100px minmax(0,1fr)}.photo-button{min-width:100px}.photo{width:100px;height:140px;object-fit:contain;background:transparent}.placeholder{background:transparent}.chart{height:140px;cursor:crosshair;touch-action:pan-y;outline-offset:5px}.chart:focus-visible{outline:2px solid var(--primary-color,#39785b);border-radius:4px}.inspection-line{stroke:var(--primary-color,#39785b);stroke-width:1;stroke-dasharray:3 3;vector-effect:non-scaling-stroke}.inspection-point{fill:var(--primary-color,#39785b);stroke:var(--ha-card-background,#fff);stroke-width:1;vector-effect:non-scaling-stroke}.graph-inspection{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:12px;padding:7px 0;min-height:32px}.graph-inspection strong{white-space:nowrap;font-size:16px}.notice:empty{display:none}.watering-details{font-size:12px;line-height:1.5;margin-top:10px}.watering-details summary{cursor:pointer;min-height:32px;align-content:center;color:var(--primary-color,#39785b)}.watering-history{list-style:none;margin:0;padding:0}.watering-event{padding:6px 0;border-bottom:1px solid var(--divider-color,#ddd)}.watering-event.retracted{color:var(--secondary-text-color,#667)}button{min-height:44px}.preview[hidden]{display:none}@media(max-width:420px){.plants{grid-template-columns:minmax(0,1fr)}.plant-tile{min-height:140px;grid-template-columns:76px minmax(0,1fr)}.tile-photo,.tile-photo .photo{width:76px;height:112px}.detail-content{padding:0 12px 14px}.dialog-header{padding:6px 12px}.body{grid-template-columns:80px minmax(0,1fr)}.photo-button{min-width:80px}.photo{width:80px;height:120px}.graph-inspection{font-size:11px}.name{font-size:18px}}\n" + '</style><ha-card><div class="header"><div class="title">Растения</div><button data-action="add">Добавить</button></div><div class="preview" hidden>ПРЕДПРОСМОТР · действия отключены</div><div class="plants"></div></ha-card><dialog class="plant-dialog" aria-labelledby="plant-detail-title"><div class="dialog-header"><strong id="plant-detail-title">Подробно о растении</strong><button data-action="close-details" aria-label="Закрыть подробности">Закрыть</button></div><div class="detail-content"></div><div class="footer"></div></dialog>';
      this._dialog = this.shadowRoot.querySelector('.plant-dialog');
      this.shadowRoot.addEventListener('click',event => {
        const button = event.target.closest?.('[data-action]');
        if (button && !button.disabled) this._action(button);
      });
      this.shadowRoot.addEventListener('pointerdown',event => {
        if (event.button !== 0 || !event.target.closest?.('.chart')) return;
        this._graphPointer = event.pointerId;
        event.target.closest('.chart').focus({preventScroll:true});
        this._inspectPointer(event);
      });
      this.shadowRoot.addEventListener('pointermove',event => {
        if (this._graphPointer === event.pointerId && event.buttons) this._inspectPointer(event);
      });
      for (const name of ['pointerup','pointercancel','pointerleave']) this.shadowRoot.addEventListener(name,() => { this._graphPointer = null; });
      this.shadowRoot.addEventListener('keydown',event => this._inspectKeyboard(event));
      this._dialog.addEventListener('close',() => this._afterClose());
      this._dialog.addEventListener('click',event => {
        if (event.target !== this._dialog) return;
        const box = this._dialog.getBoundingClientRect();
        if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) this._dialog.close();
      });
      this._shellReady = true;
    }
    _openDetails(button) {
      if (!this._plants().some(plant => plant.entity_id === button.dataset.entity)) return;
      this._rememberCalibrationSections();
      this._selectedEntity = button.dataset.entity; this._returnFocus = button;
      this._days = this._ranges.get(this._selectedEntity) || 7;
      this._loadHistory(); this._render();
      if (!this._dialog.open) this._dialog.showModal();
    }
    _afterClose() {
      this._rememberCalibrationSections(); this._selectedEntity = null; this._graphPointer = null;
      if (this._returnFocus?.isConnected) this._returnFocus.focus({preventScroll:true});
    }
    _syncPending() {
      this.shadowRoot.querySelectorAll('[data-action]').forEach(button => {
        const action = button.dataset.action;
        const key = `${action}:${button.dataset.entity || button.dataset.entry || ''}`;
        button.disabled = this._pending.has(key)
          || (Boolean(this._config.preview_status) && ['photo','snooze','speaker','voice'].includes(action))
          || (action === 'photo' && !button.dataset.entry);
      });
    }
    _render() {
      if (!this._hass) return;
      this._rememberCalibrationSections(); this._ensureShell();
      const plants = this._plants(); const preview = this._config.preview_status;
      this._patchContent(this.shadowRoot.querySelector('.plants'),plants.length
        ? plants.map(plant => this._tile(plant,preview)).join('')
        : '<div class="empty">Добавь первое растение через Adaptive Plant.</div>');
      this.shadowRoot.querySelector('.preview').hidden = !preview;
      const voice = this._hass.states['input_boolean.plant_care_voice_enabled']?.state === 'on';
      const selected = plants.find(plant => plant.entity_id === this._selectedEntity);
      this._patchContent(this.shadowRoot.querySelector('.footer'),`<button data-action="speaker">Колонка: ${esc(selected?.attributes.voice_target_name || 'выбрать')}</button><button data-action="voice">Алиса: ${voice ? 'вкл.' : 'выкл.'}</button>`);
      if (this._selectedEntity) {
        const plant = plants.find(item => item.entity_id === this._selectedEntity);
        if (plant) this._patchContent(this.shadowRoot.querySelector('.detail-content'),this._plant(plant,preview));
        else { if (this._dialog.open) this._dialog.close(); this._afterClose(); }
      }
      this._syncPending();
    }
    _choosePhoto(button) {
      if (this._config.preview_status || !button.dataset.entry || this._pending.has(`photo:${button.dataset.entry}`)) return;
      const input = document.createElement('input');
      input.type = 'file'; input.accept = 'image/*';
      input.addEventListener('change', () => { if (input.files?.[0]) this._uploadPhoto(button.dataset.entry, input.files[0]); });
      input.click(); return;
    }
    _retryHistory() {
      const source = this._hass.states[this._selectedEntity]?.attributes.source_entity;
      const old = this._history[`${source}|${this._days}`];
      if (old) old.fetchedAt = 0;
      this._loadHistory(); this._render();
    }
    async _action(button) {
      const action = button.dataset.action;
      if (action === 'open') return this._openDetails(button);
      if (action === 'close-details') return this._dialog.close();
      if (action === 'add' || action === 'options') return this._navigate();
      if (action === 'speaker' && !this._config.preview_status) return this._navigate('plant_care_alerts');
      if (action === 'photo') return this._choosePhoto(button);
      if (action === 'range') { this._days = Number(button.dataset.days); this._ranges.set(this._selectedEntity,this._days); this._loadHistory(); this._render(); return; }
      if (action === 'retry') return this._retryHistory();
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
