const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const budget = require('../render/web/render_budget.js');
const source = fs.readFileSync(require('node:path').join(__dirname, '../render/web/renderer.js'), 'utf8');
const start = source.indexOf('class SpriteRenderer {');
const end = source.indexOf('class SpriteForgeRuntime {', start);
class Display {
  constructor() { this.anchor = {set(){}}; this.texture = {}; }
  addChild() {}
}
function setup(fps = 30) {
  const ticker = {maxFPS:fps,deltaMS:1000/fps,add(fn){this.tick=fn}};
  const context = { app:{ticker}, PIXI:{Container:Display,Sprite:Display,Graphics:Display},
    window:{RenderBudget:budget},console:{log(){},warn(){},error(){}},setTimeout,clearTimeout };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+'\nglobalThis.SpriteRenderer=SpriteRenderer;',context);
  const sprite = new context.SpriteRenderer(new Display());
  sprite._queueFrameSet=()=>{}; sprite._scheduleFrameLoadPump=()=>{};
  sprite._yieldFrameLoadSlice=async()=>{}; sprite._updateMouthLayer=()=>{};
  sprite._hideMouthLayer=()=>{};
  const loads=[];
  sprite._loadTextureFromImage=async(url)=>{
    loads.push(url); return {url,height:100,width:100,baseTexture:{valid:true}};
  };
  sprite._applyFrame=texture=>{sprite.sprite.texture=texture};
  return {sprite,ticker,loads};
}
async function clip(fps=30,count=120,interval=17,closed=undefined) {
  const state=setup(fps), s=state.sprite;
  s.loadFrames('normal',Array.from({length:count},(_,i)=>'frame-'+i));
  s.setIdleFrameIntervalMs('normal',interval);
  if(closed!==undefined) {
    const openness=Array(count).fill(1);openness[closed]=0;
    s.loadMouthConfig('normal',{frameUrls:[],opennessByFrame:openness,closedFrameIdx:closed});
  }
  await s._loadFrameSet('normal',{priority:100});
  return state;
}
test('30 FPS decodes only its selected frames; endpoints and original duration remain intact',async()=>{
  const {sprite:s,loads}=await clip();
  const plan=s._frameSamplingPlans.get('normal');
  assert.equal(loads.length,plan.indices.length);
  assert.ok(loads.length<70);
  assert.ok(loads.includes('frame-0')&&loads.includes('frame-119'));
  assert.equal(s._frames.normal.length*s._frameIntervals.normal,120*17);
  assert.equal(new Set(loads).size,loads.length);
});
test('60 FPS leaves a 58.8 FPS clip intact; low-source-FPS clips are not expanded',async()=>{
  assert.equal((await clip(60)).loads.length,120);
  assert.equal((await clip(30,30,100)).loads.length,30);
});
test('selection retains source indices for mouth anchors and exact closed-mouth hold',async()=>{
  const {sprite:s}=await clip(30,120,17,72);
  const plan=s._frameSamplingPlans.get('normal');
  s._mouthConfigs.normal.anchorTrack=Array.from({length:120},(_,i)=>({cx:i,cy:0,width:10,height:10}));
  for(let i=0;i<120;i++) {
    s._showFrame(i);
    assert.equal(s._frameIdx,i);
    assert.equal(s._activeFrameIdx,plan.sourceIndex[i]);
    assert.equal(s._getMouthAnchor(s._mouthConfigs.normal).cx,plan.sourceIndex[i]);
  }
  s.holdClosedFrame();
  assert.equal(s._activeFrameIdx,72);
  assert.equal(s.sprite.texture.url,'frame-72');
});
test('an explicit hold may request an otherwise omitted frame and displays it when ready',async()=>{
  const {sprite:s}=await clip();
  const i=Array.from({length:120},(_,i)=>i).find(i=>s._sampleFrameIndex('normal',i)!==i);
  s.holdFrame(i);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(s.sprite.texture.url,'frame-'+i);
  assert.equal(s._activeFrameIdx,i);
});
test('once-then-hold completes at the original source duration within one render tick',async()=>{
  const {sprite:s,ticker}=await clip(30,120,17);
  s.setClipConfig('normal',{loopMode:'once_then_hold'});
  let completed=0;s.setCycleCompleteHandler(()=>completed++);
  let ticks=0;
  while(!completed&&ticks<100) { ticker.tick(1);ticks++; }
  assert.equal(completed,1);
  assert.equal(s._frameIdx,119);
  assert.equal(s.sprite.texture.url,'frame-119');
  assert.ok(Math.abs(ticks*ticker.deltaMS-120*17)<=ticker.deltaMS);
});
test('source mapping never strays by more than one output frame period',()=>{
  for(const interval of [5,8,17,21]) for(const fps of [30,60]) {
    const p=budget.createFrameSamplingPlan(738,interval,fps,[51]);
    for(let i=0;i<738;i++) assert.ok(Math.abs(p.sourceIndex[i]-i)*interval <= 1000/fps+interval);
    assert.equal(p.sourceIndex[51],51);
  }
});
test('time sampling does not introduce systematic repeated images at the selected render rate',async()=>{
  for(const interval of [5,8,17,21]) for(const fps of [30,60]) {
    const {sprite:s,ticker}=await clip(fps,738,interval);
    let last=s.sprite.texture,changes=0;
    for(let i=0;i<300;i++) {ticker.tick(1);if(s.sprite.texture!==last)changes++;last=s.sprite.texture;}
    const expected=300*Math.min(1,1000/interval/fps);
    assert.ok(changes>=Math.floor(expected)-5,`${fps} FPS / ${interval}ms: ${changes} changes, expected about ${expected}`);
  }
});
test('fast transitions retain authored duration and multi-frame steps cannot skip a cycle event',async()=>{
  for(const interval of [5,8,17,21]) {
    const {sprite:s,ticker}=await clip(30,120,interval);
    s.setClipConfig('normal',{loopMode:'once_then_hold'});
    let cycles=0;s.setCycleCompleteHandler(()=>cycles++);
    let ticks=0;while(!cycles&&ticks<200){ticker.tick(1);ticks++;}
    assert.equal(cycles,1);
    assert.ok(Math.abs(ticks*ticker.deltaMS-120*interval)<=ticker.deltaMS,`${interval}ms duration`);
  }
  const {sprite:s,ticker}=await clip(30,5,10);
  let cycles=0;s.setCycleCompleteHandler(()=>cycles++);
  for(let i=0;i<30;i++)ticker.tick(1);
  assert.equal(cycles,20,'each crossed 50ms loop is reported even when landing past index zero');
});
test('sampling does not reintroduce the excluded zero pose in legacy speaking loops',async()=>{
  const {sprite:s,ticker}=await clip(30,120,17);
  s.setSpeaking(true);
  for(let i=0;i<300;i++) {ticker.tick(1);assert.notEqual(s._activeFrameIdx,0);}
});
test('a cold selected hold keeps the displayed source index until the requested frame arrives',async()=>{
  const {sprite:s}=await clip();
  s._showFrame(0);
  s._frames.normal[119]=undefined;
  let finish;
  s._loadTextureFromImage=()=>new Promise(resolve=>{finish=resolve});
  s.holdFrame(119);
  assert.equal(s._activeFrameIdx,0);
  assert.equal(s.sprite.texture.url,'frame-0');
  finish({url:'frame-119',height:100,width:100,baseTexture:{valid:true}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(s._activeFrameIdx,119);
  assert.equal(s.sprite.texture.url,'frame-119');
});
test('initial fallback reports the source index of the image actually displayed',()=>{
  const {sprite:s}=setup();
  s._frameUrls.normal=Array.from({length:120},(_,i)=>'frame-'+i);
  s._frames.normal=new Array(120);
  s._frames.normal[0]={url:'frame-0',height:100,width:100,baseTexture:{valid:true}};
  s.setIdleFrameIntervalMs('normal',17);
  s._showFrame(50);
  assert.equal(s._frameIdx,50);
  assert.equal(s._activeFrameIdx,0);
  assert.equal(s.sprite.texture.url,'frame-0');
});
