const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../custom_components/chatgpt_native/panel.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise,resolve,reject}; }
function fixture() {
  const registry = {}, timers = new Map(); let next = 0;
  class Element { constructor() { this.isConnected = true; this.iframe = {}; } querySelector() { return this.iframe; } }
  vm.runInNewContext(source, {
    HTMLElement: Element, customElements: {define: (key, value) => registry[key] = value},
    setInterval: fn => { timers.set(++next, fn); return next; }, clearInterval: id => timers.delete(id),
  });
  return {panel: new registry['ubuntu-desktop-panel'](), timers};
}
const ready = () => ({callWS: async () => ({session:'test'}), callApi: async () => ({ready:true})});
(async () => {
  {
    const {panel:p,timers} = fixture(); let calls=0;
    p.isConnected=false; p.hass={...ready(),callWS:async()=>{calls++;return {session:'test'};}};
    await flush(); assert.equal(calls,0,'detached panel must not start authorization');
    p.isConnected=true; p.connectedCallback(); await flush();
    assert.match(p.innerHTML,/основной/); assert.equal(timers.size,1);
    p.hass=ready();p.connectedCallback();await flush();assert.equal(timers.size,1,'startup is idempotent');
    const params=new URL(p.iframe.src,'https://ha.example').searchParams;
    assert.equal(params.get('reconnect'),'true');
    p.isConnected=false;p.disconnectedCallback();assert.equal(timers.size,0);
    p.isConnected=true;p.connectedCallback();await flush();assert.equal(timers.size,1);
  }
  {
    const {panel:p,timers}=fixture(), old=deferred();
    p.hass={...ready(),callWS:()=>old.promise};
    p.isConnected=false;p.disconnectedCallback();p.isConnected=true;p.hass=ready();await flush();
    const html=p.innerHTML, timer=p._timer;
    old.reject(new Error('stale initial failure'));await flush();
    assert.equal(p.innerHTML,html);assert.equal(p._timer,timer);assert.ok(timers.has(timer));
  }
  {
    const {panel:p,timers}=fixture();p.hass=ready();await flush();
    const old=deferred();p._hass={...ready(),callWS:()=>old.promise};timers.get(p._timer)();
    p.isConnected=false;p.disconnectedCallback();p.isConnected=true;p.hass=ready();await flush();
    const html=p.innerHTML,timer=p._timer;
    old.reject(new Error('stale renewal failure'));await flush();
    assert.equal(p.innerHTML,html);assert.equal(p._timer,timer);assert.ok(timers.has(timer));
  }
  console.log('Physical panel: detached startup, idempotence, reconnect, and both stale-failure paths passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
