const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let Card;
const context = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {innerHTML: '',querySelectorAll:()=>[]}; } },
  customElements: {get:()=>null,define:(_name,ctor)=>{Card=ctor;}},
  window: {}, location: {origin:'http://ha.invalid'}, URL, Date, Intl, Set, Number,
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../www/plant-care/plant-care-card.js'),'utf8'),context);
const card = new Card();
card._hass = {states:{}};
const state = {entity_id:'sensor.plant',state:'sensor_error',attributes:{
  plant_name:'Стрелиция', moisture:100, untrusted_moisture:100, reliability_valid:false,
  reliability_reason:'saturated', last_value_change_at:1794538800,
  last_sample_at:Date.now()/1000, forecast_status:'invalid_readings', plant_care:true
}};
const html = card._plant(state);
assert.match(html,/Недостоверные показания/);
assert.doesNotMatch(html,/100<small> %/);
assert.match(html,/12 часов/);
assert.match(html,/не меня/);
assert.doesNotMatch(html,/данные устарели/);
assert.match(html,/Проверь грунт вручную/);
state.attributes.due_since = Date.now()/1000 - 86400;
assert.match(card._plant(state),/До ошибки сухость была подтверждена/);
state.attributes.last_sample_at = null;
state.attributes.freshness_entity = 'sensor.raw';
card._hass.states['sensor.raw'] = {attributes:{},last_reported:new Date().toISOString(),last_updated:new Date().toISOString()};
assert.match(card._plant(state),/замер: возраст неизвестен/);
console.log('Reliability card: physical freshness, hidden invalid percentage, and manual check confirmed');
