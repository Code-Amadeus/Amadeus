import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import vm from 'node:vm'

const source=await fs.readFile(new URL('../../render/web/companion_atlas.js',import.meta.url),'utf8')
const clip=(url,sequence=[0,1,2,3],size=2,columns=2)=>({url,size,columns,sequence,durationMs:2000,
  decodedBytes:Math.ceil((Math.max(...sequence)+1)/columns)*columns*size*size*4})
function manifest(){return {format:'amadeus.companion-atlas.v1',emotions:{normal:{
  idle:clip('normal/idle.webp'),idleStatic:clip('normal/still.webp',[0],2,1),
  speaking:clip('normal/talk.webp'),speakingAlternate:clip('normal/alternate.webp')},
  happy:{idle:clip('happy/idle.webp'),speaking:clip('happy/talk.webp')}}}}
function setup(data=manifest()){
 let now=0,next=0,fetchHook=null;const timers=new Map(),bitmaps=[],draws=[]
 const specs=new Map(Object.values(data.emotions).flatMap(e=>Object.values(e)).map(s=>[s.url,s]))
 const scope=vm.createContext({URL,performance:{now:()=>now},
  setTimeout:(fn,ms)=>{const id=++next;timers.set(id,{fn,at:now+ms});return id},clearTimeout:id=>timers.delete(id),
  fetch:async url=>{if(fetchHook)await fetchHook(url);const spec=specs.get(new URL(url).pathname.slice(1));
    return {ok:true,blob:async()=>({size:10,spec})}},
  createImageBitmap:async blob=>{const s=blob.spec,b={width:s.columns*s.size,height:Math.ceil((Math.max(...s.sequence)+1)/s.columns)*s.size,closed:false,close(){this.closed=true}};bitmaps.push(b);return b},
 })
 vm.runInContext(source,scope)
 const canvas={getContext:(name,options)=>{assert.equal(name,'2d');assert.equal(options.willReadFrequently,true);
  return {clearRect(){},drawImage(bitmap,...args){assert.equal(bitmap.closed,false);draws.push({now,args})}}}}
 const player=new scope.CompanionAtlas.Player(canvas,data,'http://127.0.0.1/')
 return {player,scope,draws,bitmaps,timers,setFetchHook:fn=>{fetchHook=fn},advance(ms){
  const end=now+ms;for(;;){const entry=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!entry)break;
    const [id,v]=entry;timers.delete(id);now=v.at;v.fn()}now=end
 }}
}

test('more frames retain the declared cycle duration; static idle and pause have no redraw loop',async()=>{
 const s=setup();await s.player.select('normal',true)
 s.advance(1999);assert.equal(s.draws.length,4)
 s.advance(1);assert.equal(s.draws.length,5)
 s.player.setPaused(true);s.advance(4000);assert.equal(s.draws.length,5)
 s.player.setPaused(false);s.advance(500);assert.equal(s.draws.length,6)
 await s.player.select('normal',false,true);assert.equal(s.timers.size,0)
 s.player.dispose();assert.equal(s.player.residentBytes,0);assert.ok(s.bitmaps.every(b=>b.closed))
})
test('thinking variations change between speech runs, never midway through one sentence',async()=>{
 const s=setup();await s.player.select('normal',true)
 assert.equal(s.player.active.url,'normal/talk.webp')
 await s.player.select('normal',true);assert.equal(s.player.active.url,'normal/talk.webp')
 await s.player.select('normal',false);await s.player.select('normal',true)
 assert.equal(s.player.active.url,'normal/alternate.webp')
 await s.player.select('constructor',false);assert.equal(s.player.active.url,'normal/idle.webp')
})
test('decoded resources are bounded and evicted bitmaps are explicitly closed',async()=>{
 const data=manifest()
 for(const e of Object.values(data.emotions))for(const [key,s] of Object.entries(e))e[key]=clip(s.url,[0,1,2,3,4,5,6,7],512,8)
 const s=setup(data)
 for(let i=0;i<6;i++){
  await s.player.select(i%2?'happy':'normal',Boolean(i%3))
  assert.ok(s.player.entries.size<=2);assert.ok(s.player.residentBytes<=16*1024*1024)
 }
 assert.ok(s.bitmaps.some(b=>b.closed))
})
test('superseded decode cannot replace the latest expression',async()=>{
 const s=setup();let release
 s.setFetchHook(()=>new Promise(resolve=>{release=resolve}))
 const first=s.player.select('normal',true);await Promise.resolve()
 s.setFetchHook(null);const last=s.player.select('happy',true)
 release();await Promise.all([first,last])
 assert.equal(s.player.active.url,'happy/talk.webp');assert.equal(s.bitmaps[0].closed,true)
})
test('failed shared load is visible to every caller and a later explicit change can recover',async()=>{
 const s=setup();s.setFetchHook(()=>{throw Error('fixture missing')})
 const first=s.player.select('normal',true),same=s.player.select('normal',true)
 await assert.rejects(first,/fixture missing/);await assert.rejects(same,/fixture missing/)
 s.setFetchHook(null);await s.player.select('happy',true);assert.equal(s.player.active.url,'happy/talk.webp')
})
test('manifest rejects external paths and invalid timing or decoded dimensions',()=>{
 for(const mutate of [
  s=>s.url='../private.webp',s=>s.url='https://example.com/a.webp',s=>s.url='/outside.webp',
  s=>s.durationMs=0,s=>s.sequence=[-1],s=>s.decodedBytes=0,s=>s.size=2048,
 ]){
  const data=manifest();mutate(data.emotions.normal.speaking);assert.throws(()=>setup(data))
 }
})
