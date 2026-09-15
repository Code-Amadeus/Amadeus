// Paired hardware-accelerated offscreen experiment. Run baseline|sampled and 30|60.
// Baseline renderer source is captured from the public-main revision before edits.
import {app,BrowserWindow} from 'electron'
import fs from 'node:fs/promises'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {spawn,execFileSync} from 'node:child_process'
import {createHash} from 'node:crypto'
import {createInterface} from 'node:readline'
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..')
const mode=process.argv[2]||'sampled', fps=Number(process.argv[3]||30)
if(!['baseline','sampled'].includes(mode)||![30,60].includes(fps)) throw Error('Expected baseline|sampled 30|60')
const output=path.join(root,'output/diagnostics/fps-textures',new Date().toISOString().replaceAll(':','-')+`-${mode}-${fps}`)
await fs.mkdir(output,{recursive:true})
const baselineRevision='c86177c'
const baselineSource=execFileSync('git',['show',`${baselineRevision}:render/web/renderer.js`],{cwd:root,windowsHide:true})
if(mode==='baseline')await fs.writeFile(path.join(root,'output/diagnostics/fps-textures/baseline-renderer.js'),baselineSource)
const testedSource=mode==='baseline'?baselineSource:await fs.readFile(path.join(root,'render/web/renderer.js'))
app.setPath('userData',path.join(output,'profile'))
app.on('window-all-closed',()=>{})
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))
let host,window,bridge,seq=0,paintCount=0
const pending=new Map(),samples=[]
async function startHost(){
  host=spawn(path.join(root,'.venv/Scripts/python.exe'),['-u','tools/probes/wallpaper_memory_host.py'],{
    cwd:root,windowsHide:true,env:{...process.env,GRAPHICS_PROFILE:'custom',RENDER_MAX_FPS:String(fps),RENDER_MAX_RESOLUTION:'1.5'},stdio:['pipe','pipe','pipe']})
  host.stderr.on('data',d=>{void fs.appendFile(path.join(output,'host.log'),d)})
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('Host startup timed out')),90000)
    host.once('error',reject)
    host.once('exit',code=>{if(!bridge)reject(Error(`Host exit ${code}`))})
    createInterface({input:host.stdout}).on('line',line=>{
      try {const v=JSON.parse(line);if(v.ready){bridge=v;clearTimeout(timer);resolve(v)}else if(pending.has(v.id)){pending.get(v.id)(v.result);pending.delete(v.id)}}
      catch{void fs.appendFile(path.join(output,'host.log'),line+'\n')}
    })
  })
}
const rpc=(command,params={})=>new Promise(resolve=>{
  const id=++seq;pending.set(id,resolve);host.stdin.write(JSON.stringify({id,command,...params})+'\n')
})
const js=code=>window.webContents.executeJavaScript(code)
const snapshot=`(() => {
  const s=renderApp._sprite,a=wallpaperApp.scene.app;
  const buffers=new Set(); let cpuBytes=0;
  const textures=[...new Set(Object.values(PIXI.utils.BaseTextureCache))];
  for(const t of textures) for(const value of [...Object.values(t.resource||{}),...(t.resource?._levelBuffers||[]).map(l=>l.levelBuffer)]) {
    const b=ArrayBuffer.isView(value)?value.buffer:value instanceof ArrayBuffer?value:null;
    if(b&&!buffers.has(b)){buffers.add(b);cpuBytes+=b.byteLength;}
  }
  const gl=a.renderer.gl,ext=gl.getExtension('WEBGL_debug_renderer_info');
  return {fps:a.ticker.maxFPS,resolution:a.renderer.resolution,dpr:devicePixelRatio,
    gpu:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null,
    frames:Object.values(s._frames).reduce((n,arr)=>n+arr.filter(Boolean).length,0),
    queue:s._frameLoadQueue.size,activeLoads:s._activeFrameSetLoads,cpuBufferBytes:cpuBytes,
    glTextures:a.renderer.texture.managedTextures.length,gcCount:a.renderer.textureGC.count,
    frameSets:s._frameSetStates,sampleFps:s._textureSampleFps??null,
    plans:s._frameSamplingPlans?Object.fromEntries([...s._frameSamplingPlans].map(([k,p])=>[k,p.indices.length])):null,
    ticks:window.__fpsProbe.ticks,misses:window.__fpsProbe.misses,changes:window.__fpsProbe.changes,
    cycles:window.__fpsProbe.cycles,holds:window.__fpsProbe.holds,phase:window.__fpsProbe.phase};
})()`
async function sample(stage){
  const row={stage,time:new Date().toISOString(),paints:paintCount,
    processes:app.getAppMetrics().map(p=>({pid:p.pid,type:p.type,memoryKiB:p.memory})),
    rendererPid:window.webContents.getOSProcessId(),host:await rpc('sample'),render:await js(snapshot)}
  samples.push(row);await fs.writeFile(path.join(output,'samples.json'),JSON.stringify(samples,null,2))
  console.log(JSON.stringify({stage,frames:row.render.frames,queue:row.render.queue,cpuMiB:Math.round(row.render.cpuBufferBytes/1048576),
    privateMiB:Math.round(row.processes.reduce((n,p)=>n+p.memoryKiB.privateBytes,0)/1024),paints:paintCount,output}))
}
async function phase(name){await js(`window.__fpsProbe.phase=${JSON.stringify(name)}`)}
async function run(){
 try{
  await startHost()
  window=new BrowserWindow({width:960,height:600,show:false,frame:false,transparent:true,backgroundColor:'#00000000',
    webPreferences:{offscreen:true,backgroundThrottling:false,sandbox:true,contextIsolation:true,nodeIntegration:false}})
  window.webContents.setFrameRate(60)
  window.webContents.on('paint',()=>{paintCount++})
  window.webContents.on('console-message',e=>{void fs.appendFile(path.join(output,'renderer.log'),e.message+'\n')})
  if(mode==='baseline') window.webContents.session.webRequest.onBeforeRequest({urls:['http://127.0.0.1:*/*']},(details,callback)=>{
    const u=new URL(details.url)
    if(u.pathname==='/render/web/renderer.js') {u.pathname='/output/diagnostics/fps-textures/baseline-renderer.js';callback({redirectURL:u.href})}
    else callback({})
  })
  await window.loadURL(bridge.url)
  const deadline=Date.now()+30000
  while(!await js('Boolean(window.renderApp && window.wallpaperApp?.scene?.app && renderApp._spriteforgeRuntime.rootNodeId)')){
    if(Date.now()>deadline)throw Error('Renderer not ready');await sleep(100)
  }
  await js(`(() => {
    const a=wallpaperApp.scene.app,s=renderApp._sprite,r=renderApp._spriteforgeRuntime;
    // Keep idle workload fixed, but preserve the real graph for speech/transitions.
    const nextAuto=r._nextAutoNode.bind(r);
    r._nextAutoNode=id=>id===r.rootNodeId?r.rootNodeId:nextAuto(id);
    r.cfg.closedEyeSpeakingChance=0;
    window.__fpsProbe={ticks:[],misses:[],changes:[],cycles:[],holds:[],phase:'startup'};
    let previous=performance.now(),lastTexture=null,lastHold=false;
    const show=s._showFrame.bind(s);
    s._showFrame=(i)=>{
      const label=s._currentEmotion,frames=s._frames[label]||s._frames.normal||[];
      const target=s._sampleFrameIndex?s._sampleFrameIndex(label,i%frames.length,s._sampleTimeMs):i%frames.length;
      if(!frames[target]) __fpsProbe.misses.push({t:performance.now(),phase:__fpsProbe.phase,label,index:i,target});
      show(i);
    };
    const cycle=s._cycleCompleteHandler;
    s._cycleCompleteHandler=label=>{__fpsProbe.cycles.push({t:performance.now(),label,phase:__fpsProbe.phase});cycle?.(label)};
    a.ticker.add(()=>{
      const now=performance.now();__fpsProbe.ticks.push({t:now,dt:now-previous,phase:__fpsProbe.phase});previous=now;
      if(lastTexture!==s.sprite.texture){__fpsProbe.changes.push({t:now,label:s._currentEmotion,source:s._activeFrameIdx,logical:s._frameIdx,phase:__fpsProbe.phase});lastTexture=s.sprite.texture;}
      if(lastHold!==r.postSpeechHoldActive){__fpsProbe.holds.push({t:now,phase:__fpsProbe.phase,active:r.postSpeechHoldActive,label:s._currentEmotion,source:s._activeFrameIdx});lastHold=r.postSpeechHoldActive;}
    });
  })()`)
  await fs.writeFile(path.join(output,'metadata.json'),JSON.stringify({mode,fps,baselineRevision,rendererSha256:createHash('sha256').update(testedSource).digest('hex'),versions:process.versions,offscreen:true,offscreenFrameRate:60,
    scope:'Public-main renderer with real assets; fixed idle graph choices; no voice models. Capture readback adds common overhead.',url:bridge.url},null,2))
  for(let i=1;i<=18;i++){
    await sleep(10000);await sample(`startup-${i*10}s`)
    const r=samples.at(-1).render
    if(i>=3&&r.queue===0&&r.activeLoads===0)break
    // Existing graph prefetch may revisit already cached clips every idle cycle.
    // Treat 30 seconds of unchanged resource bytes/frame count as settled too.
    if(i>=4&&samples.slice(-4).every(s=>s.render.frames===r.frames&&s.render.cpuBufferBytes===r.cpuBufferBytes))break
    if(i===18)throw Error('Preload did not settle in three minutes')
  }
  // First-use journey exercises the real runtime, including transitions and post-speech hold.
  await phase('first-speech');await rpc('speaking',{active:true});await sleep(5000)
  await phase('speech-stop');await rpc('speaking',{active:false});await sleep(3000)
  await phase('first-smile');await js(`renderApp.triggerSpriteForgeIntent('trans_smile')`);await sleep(5000)
  await phase('warm-idle');await js('renderApp._spriteforgeRuntime._playNode(renderApp._spriteforgeRuntime.rootNodeId)');await sleep(10000)
  await sample('journey-complete')
  if(samples.at(-1).render.ticks.filter(t=>t.phase==='warm-idle').length<200)throw Error('Offscreen ticker not reaching the intended rate')
  // Media encoding is outside the timing measurement phases.
  await phase('recording')
  await js(`(() => {
    const stream=wallpaperApp.scene.app.view.captureStream(30), chunks=[];
    const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp9',videoBitsPerSecond:4000000});
    window.__probeVideo={recorder,stream,done:new Promise(resolve=>{
      recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
      recorder.onstop=()=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.readAsDataURL(new Blob(chunks,{type:'video/webm'}));};
    })};recorder.start(1000);
  })()`)
  await js('renderApp._spriteforgeRuntime._playNode(renderApp._spriteforgeRuntime.rootNodeId)');await sleep(4000)
  await rpc('speaking',{active:true});await sleep(4000)
  await rpc('speaking',{active:false});await sleep(3000)
  await js(`renderApp.triggerSpriteForgeIntent('trans_smile')`);await sleep(3000)
  const data=await js('(__probeVideo.recorder.stop(),__probeVideo.done)')
  await fs.writeFile(path.join(output,'animation.webm'),Buffer.from(data,'base64'))
  await js('__probeVideo.stream.getTracks().forEach(t=>t.stop())')
  await fs.writeFile(path.join(output,'frame.png'),(await window.webContents.capturePage()).toPNG())
  await sample('recording-complete')
 }finally{
  window?.destroy()
  if(host&&host.exitCode===null){host.stdin.end(JSON.stringify({command:'stop'})+'\n');await Promise.race([new Promise(r=>host.once('exit',r)),sleep(5000)]);if(host.exitCode===null)host.kill()}
 }
}
app.whenReady().then(run).then(()=>app.exit(0),e=>{console.error(e);app.exit(1)})
