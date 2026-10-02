import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import vm from 'node:vm'

const projection=await fs.readFile(new URL('../../render/web/companion_presentation.js',import.meta.url),'utf8')
const panel=await fs.readFile(new URL('../../render/web/companion_panel.js',import.meta.url),'utf8')
async function setup(input=null){
 let now=0,next=0,source
 const timers=new Map(),elements=new Map()
 const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},attributes:{},setAttribute(name,value){this.attributes[name]=value},addEventListener(){},matches(){return false}});return elements.get(id)}
 const window={companion:{portraits:async()=>({normal:{idle:['normal.png']}}),connected(){},...(input?{input}:{})},addEventListener(){}}
 const scope=vm.createContext({window,console,URLSearchParams,location:{search:'?bridgePort=17797'},
  localStorage:{getItem:()=>null},document:{hidden:false,fonts:{ready:Promise.resolve()},getElementById:element,addEventListener(){},body:{classList:{toggle(){}}}},
  ResizeObserver:class {observe(){} disconnect(){}},
  EventSource:class {constructor(){source=this}},setInterval:()=>0,clearInterval(){},
  setTimeout:(fn,delay)=>{const id=++next;timers.set(id,{fn,at:now+delay});return id},clearTimeout:id=>timers.delete(id),
 })
 vm.runInContext(projection,scope);window.CompanionPresentation=scope.CompanionPresentation
 vm.runInContext(panel,scope);await new Promise(resolve=>setImmediate(resolve));source.onopen()
 return {element,disconnect(){source.onerror()},reconnect(){source.onopen()},send(method,value){source.onmessage({data:JSON.stringify({method,args:[value]})})},advance(ms){
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

const flush=()=>new Promise(resolve=>setImmediate(resolve))
test('disconnect cancels unsent input clicks even when status completes after reconnect',async()=>{
 for(const name of ['voice','vision'])for(const background of [false,true])for(const reconnect of [false,true]){
  const snapshot={ok:true,supports_images:true,watching:false,voice:{active:false,source:''}}
  const actions=[];let hold=false,release
  const s=await setup(async action=>{
   if(action==='status'){
    if(hold){hold=false;return new Promise(resolve=>{release=()=>resolve(structuredClone(snapshot))})}
    return structuredClone(snapshot)
   }
   actions.push(action);return {ok:true}
  })
  await flush();hold=true
  if(background)s.send('setAsrStatus',{status:'idle'})
  s.element(name).onclick();await flush()
  s.disconnect();if(reconnect)s.reconnect()
  release();await flush()
  assert.deepEqual(actions,[],`${name}: background=${background}, reconnect=${reconnect}`)
  if(!reconnect)s.reconnect()
  await flush()
  assert.equal(s.element(name).disabled,false,'a fresh click must remain available after reconnect')
  s.element(name).onclick();await flush()
  assert.deepEqual(actions,[name==='voice'?'voice_start':'vision_toggle'])
 }
})

test('a pre-disconnect status result cannot replace the new connection state',async()=>{
 const snapshot={ok:true,supports_images:true,watching:false,voice:{active:false,source:''}}
 let hold=false,release
 const s=await setup(async()=>{
  const result=structuredClone(snapshot)
  if(hold){hold=false;return new Promise(resolve=>{release=()=>resolve(result)})}
  return result
 })
 await flush();hold=true;s.send('setAsrStatus',{status:'idle'})
 s.disconnect();snapshot.voice={active:true,source:'other'};s.reconnect()
 release();await flush()
 assert.equal(s.element('voice').disabled,true)
 assert.equal(s.element('vision').disabled,false,'new connection must receive fresh status')
})

test('input buttons follow Host completion and reject duplicate clicks while pending',async()=>{
 const snapshot={ok:true,supports_images:true,watching:false,voice:{active:false,source:''}}
 const actions=[];let release
 const s=await setup(async action=>{
  if(action==='status')return structuredClone(snapshot)
  actions.push(action)
  return new Promise(resolve=>{release=resolve})
 })
 await flush();s.send('setSubtitle','保留当前字幕')
 s.element('voice').onclick();await flush()
 assert.equal(s.element('voice').disabled,true)
 assert.equal(s.element('vision').disabled,true)
 s.element('voice').onclick();s.element('vision').onclick()
 assert.deepEqual(actions,['voice_start'])
 assert.equal(s.element('voice').attributes['aria-pressed'],'false')
 snapshot.voice={active:true,source:'wake'};release({ok:true});await flush()
 assert.equal(s.element('voice').attributes['aria-pressed'],'true')
 assert.equal(s.element('voice').disabled,false)
 assert.equal(s.element('caption').textContent,'保留当前字幕')
})

test('reconnect reconciles an already dispatched input without replaying it',async()=>{
 const snapshot={ok:true,supports_images:true,watching:false,voice:{active:false,source:''}}
 const actions=[];let release
 const s=await setup(async action=>{
  if(action==='status')return structuredClone(snapshot)
  actions.push(action);return new Promise(resolve=>{release=resolve})
 })
 await flush();s.element('voice').onclick();await flush()
 s.disconnect();s.reconnect()
 snapshot.voice={active:true,source:'wake'};release({ok:true});await flush()
 assert.deepEqual(actions,['voice_start'])
 assert.equal(s.element('voice').attributes['aria-pressed'],'true')
 assert.equal(s.element('voice').disabled,false)
})
test('input ownership, model limitations and rejected changes remain Host facts across reconnect',async()=>{
 const snapshot={ok:true,supports_images:false,watching:false,voice:{active:true,source:'other'}}
 const actions=[]
 const s=await setup(async action=>{
  if(action==='status')return structuredClone(snapshot)
  actions.push(action);return {ok:false,error:'fixture_denied'}
 })
 await flush();s.send('setSubtitle','控制错误不替换字幕')
 assert.equal(s.element('voice').disabled,true)
 assert.equal(s.element('vision').disabled,true)
 s.element('voice').onclick();s.element('vision').onclick();assert.deepEqual(actions,[])
 snapshot.voice={active:false,source:''};snapshot.supports_images=true
 s.send('setAsrStatus',{status:'idle',source:'wake'});await flush()
 assert.equal(s.element('vision').disabled,false)
 s.element('vision').onclick();await flush()
 assert.deepEqual(actions,['vision_toggle'])
 assert.equal(s.element('vision').attributes['aria-pressed'],'false')
 assert.match(s.element('vision').title,/fixture_denied/)
 assert.equal(s.element('caption').textContent,'控制错误不替换字幕')
 s.disconnect();assert.equal(s.element('voice').disabled,true)
 s.reconnect();await flush();assert.deepEqual(actions,['vision_toggle'])
})
