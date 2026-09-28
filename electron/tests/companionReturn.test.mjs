import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import vm from 'node:vm'

const projection=await fs.readFile(new URL('../../render/web/companion_presentation.js',import.meta.url),'utf8')
const panel=await fs.readFile(new URL('../../render/web/companion_panel.js',import.meta.url),'utf8')
async function setup(){
 let now=0,next=0,source
 const timers=new Map(),elements=new Map()
 const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},setAttribute(){}});return elements.get(id)}
 const window={companion:{portraits:async()=>({normal:{idle:['normal.png']}}),connected(){}},addEventListener(){}}
 const scope=vm.createContext({window,console,URLSearchParams,location:{search:'?bridgePort=17797'},
  localStorage:{getItem:()=>null},document:{hidden:false,getElementById:element,addEventListener(){},body:{classList:{toggle(){}}}},
  EventSource:class {constructor(){source=this}},setInterval:()=>0,clearInterval(){},
  setTimeout:(fn,delay)=>{const id=++next;timers.set(id,{fn,at:now+delay});return id},clearTimeout:id=>timers.delete(id),
 })
 vm.runInContext(projection,scope);window.CompanionPresentation=scope.CompanionPresentation
 vm.runInContext(panel,scope);await new Promise(resolve=>setImmediate(resolve));source.onopen()
 return {element,send(method,value){source.onmessage({data:JSON.stringify({method,args:[value]})})},advance(ms){
  now+=ms;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}
 }}
}
test('speech completion returns to neutral after a short hold without losing the spoken line',async()=>{
 const s=await setup();s.send('setSubtitle','保留这句话');s.send('setEmotion','happy');s.send('setSpeaking',true)
 s.send('setSpeaking',false);s.send('setSubtitle','');s.advance(349)
 assert.equal(s.element('portrait').dataset.emotion,'happy')
 s.advance(1);assert.equal(s.element('portrait').dataset.emotion,'normal')
 assert.equal(s.element('caption').textContent,'保留这句话')
})
test('new speech and new expression each cancel a stale return deadline',async()=>{
 for(const event of [['setSpeaking',true],['setEmotion','thinking']]){
  const s=await setup();s.send('setEmotion','happy');s.send('setSpeaking',true);s.send('setSpeaking',false)
  s.advance(200);s.send(...event);s.advance(500)
  assert.equal(s.element('portrait').dataset.emotion,event[0]==='setEmotion'?'sided_thinking':'happy')
 }
})
