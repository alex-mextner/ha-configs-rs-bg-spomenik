const fs = require('node:fs'); const vm = require('node:vm'); const assert = require('node:assert/strict');
let Card;
const context={HTMLElement:class{attachShadow(){this.shadowRoot={innerHTML:'',querySelectorAll:()=>[]};}},
  customElements:{get:()=>undefined,define:(n,c)=>Card=c},window:{},location:{origin:'http://ha.local'},URL,Intl,Date,console};
vm.runInNewContext(fs.readFileSync('www/plant-care/plant-care-card.js','utf8'),context);
function render(status='ok',preview, name='Стрелиция',picture=''){
  const card=new Card();
  card.setConfig({preview_status:preview});
  const plant={entity_id:'sensor.test',state:status,attributes:{plant_care:true,plant_name:name,moisture:55.2,picture,last_watering_at:'2026-10-26',last_watering_source:'user_report'}};
  card._hass={states:{'sensor.test':plant,'input_boolean.plant_care_voice_enabled':{state:'on'}}};
  return {tile:card._tile(plant,preview),detail:card._plant(plant,preview)};
}
assert.match(render().tile,/55<small>/); assert.match(render().tile,/Показания влажности/);
assert.match(render('due').detail,/class="plant due"/); assert.match(render('overdue').detail,/Полив просрочен больше суток/);
assert.match(render('sensor_error').detail,/Полив по этим данным не определяется/);
assert.match(render('ok','due').tile,/plant-tile due/); assert.match(render('ok','due').detail,/data-entity="sensor.test" disabled/);
assert.ok(!render('ok',undefined,'<img src=x onerror=alert(1)>').detail.includes('<h3 class="name"><img'));
assert.ok(!render('ok',undefined,'Plant','javascript:alert(1)').tile.includes('src="javascript:'));
assert.match(render('ok',undefined,'Plant','/local/adaptive_plant/owner.jpg').tile,/http:\/\/ha.local\/local\/adaptive_plant\/owner.jpg/);
console.log('PASS: 10 card assertions (tile/detail states, safe photo/name, read-only previews)');
