const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const registry=new Map(),writes=[],local=[];
class HTMLElement {}
const context={HTMLElement,customElements:{define:(n,c)=>registry.set(n,c)},navigator:{clipboard:{readText:async()=> 'с телефона 👌',writeText:async t=>local.push(t)}},TextEncoder,console,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync('custom_components/chatgpt_native/panel.js','utf8'),context);
(async()=>{
  const panel=new (registry.get('chatgpt-native-panel'))();
  assert.equal(typeof panel.clipboardAction,'function','Clipboard actions must exist');
  const area={value:'',focus(){},select(){}},status={textContent:''};
  panel.querySelector=s=>s==='[data-clip-text]'?area:status;
  panel._hass={callApi:async(method,path,data)=>{writes.push({method,path,data});return {text:'с Ubuntu <b>не HTML</b> 👌'};}};
  await panel.clipboardAction('local',()=>true);assert.equal(area.value,'с телефона 👌');
  await panel.clipboardAction('send',()=>true);assert.equal(writes.at(-1).data.text,'с телефона 👌');
  await panel.clipboardAction('receive',()=>true);assert.equal(area.value,'с Ubuntu <b>не HTML</b> 👌');
  await panel.clipboardAction('copy',()=>true);assert.equal(local[0],area.value);
  panel._hass.callApi=async()=>{throw {status_code:423}};
  await panel.clipboardAction('receive',()=>true);assert.match(status.textContent,/разблокир/i);
  delete context.navigator.clipboard;
  await panel.clipboardAction('local',()=>true);assert.match(status.textContent,/вручную/i);
  console.log('Clipboard buttons: both directions, Unicode, locked denial, and manual fallback passed');
})().catch(e=>{console.error(e);process.exitCode=1});
