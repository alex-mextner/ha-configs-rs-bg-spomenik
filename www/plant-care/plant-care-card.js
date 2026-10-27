// SPDX-License-Identifier: MIT
// A small, auto-discovering companion card. Plant photos/settings belong to Adaptive Plant.
(() => {
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const words = {
    ok: ['Всё в порядке', 'mdi:check-circle-outline'],
    checking: ['Проверяю сухость', 'mdi:timer-sand'],
    due: ['Пора поливать', 'mdi:watering-can-outline'],
    overdue: ['Полив просрочен', 'mdi:alert-circle-outline'],
    snoozed: ['Напоминание отложено', 'mdi:bell-sleep-outline'],
    sensor_error: ['Нет свежих данных', 'mdi:access-point-off'],
  };
  const dateLabel = value => {
    if (!value) return 'ещё не отмечен';
    const d = new Date(value.length === 10 ? value + 'T12:00:00' : value);
    return Number.isNaN(d.getTime()) ? 'ещё не отмечен' : new Intl.DateTimeFormat('ru-RU', {day:'numeric',month:'long'}).format(d);
  };
  const imageURL = value => {
    if (!value) return '';
    try {
      const url = new URL(value, location.origin);
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
    } catch (_) { return ''; }
  };

  class PlantCareCard extends HTMLElement {
    constructor() { super(); this.attachShadow({mode:'open'}); this._config = {}; }
    setConfig(config) { this._config = config || {}; this._previousPlants = null; this._render(); }
    static getStubConfig() { return {type:'custom:plant-care-card'}; }
    getCardSize() { return 5; }
    getGridOptions() { return {columns:12,min_columns:6,rows:'auto'}; }
    set hass(hass) { this._hass = hass; this._render(); }
    get hass() { return this._hass; }

    _plants() {
      if (!this._hass) return [];
      return Object.values(this._hass.states).filter(s => s.attributes?.plant_care === true &&
        (!this._config.entities || this._config.entities.includes(s.entity_id)))
        .sort((a,b) => (a.attributes.plant_name || '').localeCompare(b.attributes.plant_name || '', 'ru'));
    }
    _navigate() {
      const path = '/config/integrations/integration/adaptive_plant';
      history.pushState(null, '', path);
      window.dispatchEvent(new Event('location-changed'));
    }
    _render() {
      if (!this._hass) return;
      const plants = this._plants();
      const preview = this._config.preview_status;
      const voice = this._hass.states['input_boolean.plant_care_voice_enabled']?.state === 'on';
      if (this._previousPlants?.length === plants.length && this._previousPlants.every((p,i) => p === plants[i]) && this._preview === preview && this._voice === voice) return;
      this._previousPlants = plants; this._preview = preview; this._voice = voice;
      const warnings = plants.filter(p => ['due','overdue'].includes(preview || p.state)).length;
      this.shadowRoot.innerHTML = `
        <style>
          :host{display:block;font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif)}
          *{box-sizing:border-box}ha-card{overflow:hidden;border-radius:22px;background:var(--ha-card-background,var(--card-background-color,#fff));box-shadow:none;border:1px solid var(--divider-color,#e6e9e6);color:var(--primary-text-color,#1e2922)}
          .header{display:flex;justify-content:space-between;align-items:center;padding:20px 20px 16px;gap:10px}.title{display:flex;align-items:center;gap:10px;font-size:21px;font-weight:650}.title ha-icon{color:#45816b}.sub{color:var(--secondary-text-color,#6d7771);font-size:12px;margin-top:5px}
          button{font:inherit;cursor:pointer;border:0;min-height:42px;border-radius:11px;padding:9px 12px;display:inline-flex;align-items:center;justify-content:center;gap:6px;background:transparent;color:inherit}button:hover{filter:brightness(.95);background:color-mix(in srgb,currentColor 5%,transparent)}button:focus-visible{outline:2px solid var(--primary-color,#45816b);outline-offset:2px}button:disabled{cursor:default;opacity:.55}.add{color:#38765b;font-size:13px;font-weight:600;background:color-mix(in srgb,#45816b 9%,transparent);white-space:nowrap}.add ha-icon{--mdc-icon-size:18px}
          .plants{display:grid;gap:12px;padding:0 16px 16px}.plant{border:1px solid var(--divider-color,#e6e9e6);border-radius:17px;overflow:hidden;--accent:#39785b;--tint:rgba(72,128,97,.08)}.plant.due{--accent:#9a4b00;--tint:rgba(245,158,11,.14);border-color:#edb552}.plant.overdue{--accent:#b42318;--tint:rgba(220,50,38,.1);border-color:#e68f86}.plant.sensor_error{--accent:#6a6f76;--tint:rgba(100,116,139,.1)}.plant.checking,.plant.snoozed{--accent:#576a7c;--tint:rgba(100,116,139,.08)}
          .body{display:grid;grid-template-columns:106px 1fr;gap:16px;padding:16px}.photo{width:106px;height:144px;object-fit:cover;border-radius:12px;background:#eff3ed}.placeholder{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:9px;background:var(--tint);color:var(--accent)}.placeholder ha-icon{--mdc-icon-size:47px}.placeholder span{font-size:10px;text-align:center;opacity:.8}.name{font-size:21px;font-weight:650;line-height:1.2;margin:0 0 9px}.status{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:650;border-radius:99px;padding:5px 8px;color:var(--accent);background:var(--tint)}.status ha-icon{--mdc-icon-size:14px}
          .moisture{font-size:34px;font-weight:650;letter-spacing:-1px;margin-top:10px;line-height:1.05}.moisture small{font-size:19px;font-weight:500;letter-spacing:0}.caption{color:var(--secondary-text-color,#728075);font-size:11px;margin-top:4px}.watered{font-size:12px;color:var(--secondary-text-color,#6d7771);margin-top:10px}.watered strong{font-weight:500;color:var(--primary-text-color,#1e2922)}
          .notice{padding:11px 16px;background:var(--tint);color:var(--accent);border-top:1px solid color-mix(in srgb,var(--accent) 18%,transparent);font-size:12px;line-height:1.5}.notice strong{display:block;font-weight:650}.actions{display:flex;gap:6px;flex-wrap:wrap;padding:12px 14px;border-top:1px solid var(--divider-color,#e6e9e6)}.actions button{font-size:12px;min-height:38px;padding:8px 10px}.actions ha-icon{--mdc-icon-size:17px}.primary{background:var(--tint);color:var(--accent);font-weight:600}.settings{margin-left:auto;color:var(--secondary-text-color,#6d7771)}
          .footer{border-top:1px solid var(--divider-color,#e6e9e6);display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 18px;color:var(--secondary-text-color,#6d7771);font-size:11px}.voice{min-height:32px;padding:5px 7px;font-size:11px}.voice ha-icon{--mdc-icon-size:16px}.preview{margin:0 16px 13px;padding:10px 12px;border:1px dashed #8a97a5;border-radius:10px;color:#526579;font-size:11px;font-weight:600}.empty{padding:20px;text-align:center;color:var(--secondary-text-color)}
          @media(max-width:420px){.header{padding:16px}.title{font-size:19px}.body{grid-template-columns:84px 1fr;gap:12px;padding:14px}.photo{width:84px;height:139px}.name{font-size:19px}.plants{padding:0 10px 12px}.actions{gap:2px;padding:10px}.actions button{font-size:11px;padding:8px}.settings{margin-left:0}.footer{padding:8px 12px}.sub{font-size:11px}}
        </style>
        <ha-card>
          <div class="header"><div><div class="title"><ha-icon icon="mdi:sprout"></ha-icon>Растения</div><div class="sub">${warnings ? `Нужен полив · ${warnings}` : 'Уход без лишних напоминаний'}</div></div><button class="add" data-action="add"><ha-icon icon="mdi:plus"></ha-icon>Добавить</button></div>
          ${preview ? '<div class="preview">ПРЕДПРОСМОТР · данные растения не изменены</div>' : ''}
          <div class="plants">${plants.length ? plants.map(s => this._plant(s,preview)).join('') : '<div class="empty">Добавь первое растение через Adaptive Plant.</div>'}</div>
          <div class="footer"><span>Просрочка &gt; суток · напоминания 08–22</span><button class="voice" data-action="voice" ${preview ? 'disabled' : ''}><ha-icon icon="mdi:account-voice"></ha-icon>Алиса: ${voice ? 'вкл.' : 'выкл.'}</button></div>
        </ha-card>`;
      this.shadowRoot.querySelectorAll('[data-action]').forEach(button => {
        button.addEventListener('click', () => this._action(button));
      });
    }
    _plant(state, preview) {
      const a = state.attributes;
      const status = words[preview || state.state] ? (preview || state.state) : 'sensor_error';
      const [label, icon] = words[status];
      const picture = imageURL(a.picture);
      const moisture = preview === 'due' || preview === 'overdue' ? 18 : a.moisture;
      let notice = '';
      if (status === 'due') notice = '<strong>Пора проверить грунт и полить.</strong>Сухость подтверждена. После полива нажми «Полил».';
      if (status === 'overdue') notice = '<strong>Полив просрочен больше суток.</strong>Алиса напомнит раз в час в дневное время.';
      if (status === 'sensor_error') notice = '<strong>Нет свежих показаний датчика.</strong>Это не просьба поливать: сначала проверь датчик.';
      if (status === 'checking') notice = `Влажность снизилась. Проверяю, сохранится ли сухость ${esc(a.confirmation_hours || 6)} ч, чтобы не реагировать на шум.`;
      if (status === 'snoozed') notice = 'Напоминания приостановлены на сутки. Показания продолжают обновляться.';
      return `<section class="plant ${status}"><div class="body">
        ${picture ? `<img class="photo" src="${esc(picture)}" alt="${esc(a.plant_name)}" loading="lazy">` : '<div class="photo placeholder"><ha-icon icon="mdi:sprout"></ha-icon><span>Добавь своё фото<br>в настройках</span></div>'}
        <div><h3 class="name">${esc(a.plant_name)}</h3><span class="status"><ha-icon icon="${icon}"></ha-icon>${label}</span><div class="moisture">${status === 'sensor_error' || moisture == null ? '—' : esc(Math.round(moisture)) + '<small> %</small>'}</div><div class="caption">относительная влажность · сглажено</div><div class="watered">Последний полив: <strong>${esc(dateLabel(a.last_watered))}</strong></div></div>
        </div>${notice ? `<div class="notice">${notice}</div>` : ''}
        <div class="actions"><button class="primary" data-action="water" data-entity="${esc(a.button_water)}" ${preview || !a.button_water ? 'disabled' : ''}><ha-icon icon="mdi:watering-can-outline"></ha-icon>Полил</button><button data-action="snooze" data-entity="${esc(a.button_snooze)}" ${preview || !a.button_snooze ? 'disabled' : ''}><ha-icon icon="mdi:bell-sleep-outline"></ha-icon>На завтра</button><button class="settings" data-action="options"><ha-icon icon="mdi:image-plus-outline"></ha-icon>Фото и настройки</button></div></section>`;
    }
    async _action(button) {
      const action = button.dataset.action;
      if (action === 'add' || action === 'options') return this._navigate();
      if (this._config.preview_status) return;
      button.disabled = true;
      try {
        if (action === 'voice') await this._hass.callService('input_boolean','toggle',{entity_id:'input_boolean.plant_care_voice_enabled'});
        else {
          await this._hass.callService('button','press',{entity_id:button.dataset.entity});
          await this._hass.callService('plant_care_alerts','refresh',{});
        }
      } catch (error) {
        this.dispatchEvent(new CustomEvent('hass-notification',{bubbles:true,composed:true,detail:{message:'Действие не выполнено. Проверь соединение с Home Assistant.'}}));
      } finally { button.disabled = false; }
    }
  }
  if (!customElements.get('plant-care-card')) customElements.define('plant-care-card',PlantCareCard);
  window.customCards = window.customCards || [];
  if (!window.customCards.some(c => c.type === 'plant-care-card')) window.customCards.push({type:'plant-care-card',name:'Растения — полив',description:'Фото, влажность, оранжевые и красные предупреждения. Растения добавляются автоматически.',preview:true});
})();
