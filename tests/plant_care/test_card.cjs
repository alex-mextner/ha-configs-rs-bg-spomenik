const fs = require('node:fs'); const vm = require('node:vm'); const assert = require('node:assert/strict');
let Card;
const context={HTMLElement:class{attachShadow(){this.shadowRoot={innerHTML:'',querySelectorAll:()=>[]};}},
  customElements:{get:()=>undefined,define:(n,c)=>Card=c},window:{},location:{origin:'http://ha.local'},URL,Intl,Date,console};
vm.runInNewContext(fs.readFileSync('www/plant-care/plant-care-card.js','utf8'),context);
function render(status='ok',preview, name='Стрелиция',picture=''){
  const c=new Card(); c.setConfig({preview_status:preview}); c.hass={states:{'sensor.test':{entity_id:'sensor.test',state:status,attributes:{plant_care:true,plant_name:name,moisture:55.2,picture,last_watered:'2026-10-26',button_water:'button.water',button_snooze:'button.snooze'}},'input_boolean.plant_care_voice_enabled':{state:'on'}}}; return c.shadowRoot.innerHTML;
}
assert.match(render(),/55<small>/); assert.match(render(),/Всё в порядке/);
assert.match(render('due'),/class="plant due"/); assert.match(render('overdue'),/Полив просрочен больше суток/);
assert.match(render('sensor_error'),/Это не просьба поливать/);
assert.match(render('ok','due'),/ПРЕДПРОСМОТР/); assert.match(render('ok','due'),/data-entity="button.water" disabled/);
assert.ok(!render('ok',undefined,'<img src=x onerror=alert(1)>').includes('<h3 class="name"><img'));
assert.ok(!render('ok',undefined,'Plant','javascript:alert(1)').includes('src="javascript:'));
assert.match(render('ok',undefined,'Plant','/local/adaptive_plant/owner.jpg'),/http:\/\/ha.local\/local\/adaptive_plant\/owner.jpg/);
console.log('PASS: 10 card assertions (states, safe photo/name, read-only previews)');
