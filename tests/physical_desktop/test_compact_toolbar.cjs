const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('custom_components/chatgpt_native/panel.js','utf8'),registry={};
class Element {constructor(){this.isConnected=true;}}
vm.runInNewContext(source,{HTMLElement:Element,customElements:{define:(n,c)=>registry[n]=c},TextEncoder,setInterval,clearInterval});
const p=new registry['ubuntu-desktop-panel'](),events=[];
const canvas={focus(){},dispatchEvent(e){events.push([e.type,e.key,e.ctrlKey]);}};
class KeyboardEvent {constructor(type,o){this.type=type;Object.assign(this,o);}}
p.querySelector=()=>({contentWindow:{KeyboardEvent},contentDocument:{querySelector:()=>canvas,documentElement:{classList:{contains:()=>true}},getElementById:()=>({checked:false})}});
p.sendRemoteShortcut('c');assert.deepEqual(events,[['keydown','Control',true],['keydown','c',true],['keyup','c',true],['keyup','Control',false]]);
p.sendRemoteShortcut('v');assert.equal(events[5][1],'v');
assert.match(source,/--ha-toolbar-height:28px/);assert.match(source,/data-tabler/);assert.doesNotMatch(source,/📋|height:64px/);
console.log('Remote Ctrl+C/V and compact Tabler toolbar passed');
