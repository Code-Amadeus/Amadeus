// Isolated shutdown controls. Run with Node, which captures the Electron child's
// final stdout/stderr through the child "close" event, including app.exit teardown.
// Example: node tools/probes/frame-video/shutdown_control.mjs --electron <electron.exe>
//   --output <NEW_DIRECTORY> --mode blank --seconds 2 --trace --high-performance-gpu
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(here), '../../..');
const modes = ['blank', 'webgl', 'pixi', 'decode', 'video'];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const deadline = async (promise, ms, label) => {
  let timer;
  try { return await Promise.race([promise, new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(Error(label + ' timed out')), ms);
  })]); } finally { clearTimeout(timer); }
};

export function parseOptions(args) {
  const { values: v } = parseArgs({ args, options: {
    child: { type: 'boolean', default: false }, electron: { type: 'string' }, output: { type: 'string' },
    mode: { type: 'string', default: 'blank' }, fixture: { type: 'string' },
    seconds: { type: 'string', default: '2' }, 'decode-frames': { type: 'string', default: '4' },
    hardware: { type: 'string', default: 'prefer-hardware' }, 'context-loss': { type: 'boolean', default: false },
    cleanup: { type: 'string', default: 'page' }, finish: { type: 'boolean', default: false },
    'exit-method': { type: 'string', default: 'exit' }, 'settle-ms': { type: 'string', default: '250' },
    'timeout-ms': { type: 'string', default: '60000' }, trace: { type: 'boolean', default: false },
    'gpu-info': { type: 'boolean', default: false },
    'high-performance-gpu': { type: 'boolean', default: false },
  } });
  const options = { child: v.child, electron: v.electron, output: v.output, mode: v.mode, fixture: v.fixture,
    seconds: Number(v.seconds), decodeFrames: Number(v['decode-frames']), hardware: v.hardware,
    contextLoss: v['context-loss'], cleanup: v.cleanup, finish: v.finish, exitMethod: v['exit-method'],
    settleMs: Number(v['settle-ms']), timeoutMs: Number(v['timeout-ms']), trace: v.trace, gpuInfo: v['gpu-info'],
    highPerformanceGpu: v['high-performance-gpu'] };
  if (!options.output || (!options.child && !options.electron)) throw Error('Required: --electron PATH --output NEW_DIRECTORY');
  if (!modes.includes(options.mode) || !Number.isFinite(options.seconds) || options.seconds < 0 || options.seconds > 60
      || !Number.isInteger(options.decodeFrames) || options.decodeFrames < 1 || options.decodeFrames > 64
      || !Number.isInteger(options.settleMs) || options.settleMs < 0 || options.settleMs > 10000
      || !Number.isInteger(options.timeoutMs) || options.timeoutMs < 1000 || options.timeoutMs > 180000
      || !['exit', 'quit'].includes(options.exitMethod) || !['page', 'window'].includes(options.cleanup)
      || !['prefer-hardware', 'prefer-software', 'no-preference'].includes(options.hardware)) throw Error('Invalid control options');
  if (['decode', 'video'].includes(options.mode) && !options.fixture) throw Error('Decode/video controls require --fixture');
  if (options.contextLoss && ['blank', 'decode'].includes(options.mode)) throw Error('Context loss requires a WebGL control');
  return options;
}

export function childArgs(options) {
  const args = [here, '--child', '--output', path.resolve(options.output), '--mode', options.mode,
    '--seconds', String(options.seconds), '--decode-frames', String(options.decodeFrames), '--hardware', options.hardware,
    '--cleanup', options.cleanup, '--exit-method', options.exitMethod, '--settle-ms', String(options.settleMs),
    '--timeout-ms', String(options.timeoutMs)];
  if (options.fixture) args.push('--fixture', path.resolve(options.fixture));
  for (const [value, flag] of [[options.contextLoss, '--context-loss'], [options.finish, '--finish'],
    [options.trace, '--trace'], [options.gpuInfo, '--gpu-info'], [options.highPerformanceGpu, '--high-performance-gpu']]) if (value) args.push(flag);
  return args;
}

export function classifyExit({ exitCode, signal, timedOut, events, stderrLines, childResult }) {
  const gpuErrors = stderrLines.filter(line => line.text.includes('GPU state invalid after WaitForGetOffsetInRange'));
  const nativeErrors = stderrLines.filter(line => /\bERROR\b/.test(line.text));
  const complete = Boolean(childResult?.completed) && exitCode === 0 && !signal && !timedOut;
  return { outcome: gpuErrors.length ? 'gpu-state-invalid-observed' : complete ? 'no-gpu-state-invalid-observed' : 'control-incomplete',
    childExitCode: exitCode, signal, timedOut, captureComplete: !timedOut && exitCode !== null,
    gpuStateInvalidCount: gpuErrors.length, nativeErrorCount: nativeErrors.length, gpuErrors, nativeErrors,
    appExitRequested: events.find(event => event.name === 'app-exit-requested') ?? null,
    stabilityValidated: false,
    limitation: 'A control distinguishes exercised components; one clean exit does not establish long-term stability. Stderr receipt stages do not prove native emission time.' };
}

async function launch(options) {
  const output = path.resolve(options.output);
  await fs.mkdir(output, { recursive: false });
  const stdout = await fs.open(path.join(output, 'stdout.log'), 'wx'), stderr = await fs.open(path.join(output, 'stderr.log'), 'wx');
  const events = [{ name: 'launcher-start', atEpochMs: Date.now() }], stderrLines = [];
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const args = childArgs(options);
  const child = spawn(options.electron, args, { cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdoutPending = '', stderrPending = '', timedOut = false, spawnError, lastStage = 'launcher-start';
  let stdoutWrites = Promise.resolve(), stderrWrites = Promise.resolve();
  const stdoutLine = text => {
    if (!text.startsWith('CONTROL_EVENT ')) return;
    try {
      const event = JSON.parse(text.slice('CONTROL_EVENT '.length));
      events.push({ ...event, receivedEpochMs: Date.now() }); lastStage = event.name;
    } catch (error) { events.push({ name: 'event-parse-error', text, error: String(error), atEpochMs: Date.now() }); }
  };
  const stderrLine = text => stderrLines.push({ text, receivedEpochMs: Date.now(), lastStageAtReceipt: lastStage });
  child.stdout.on('data', bytes => {
    stdoutWrites = stdoutWrites.then(() => stdout.write(bytes)); stdoutPending += bytes.toString('utf8');
    const lines = stdoutPending.split(/\r?\n/); stdoutPending = lines.pop(); lines.forEach(stdoutLine);
  });
  child.stderr.on('data', bytes => {
    stderrWrites = stderrWrites.then(() => stderr.write(bytes)); stderrPending += bytes.toString('utf8');
    const lines = stderrPending.split(/\r?\n/); stderrPending = lines.pop(); lines.forEach(stderrLine);
  });
  child.on('error', error => { spawnError = String(error); });
  const timer = setTimeout(() => {
    timedOut = true; events.push({ name: 'launcher-timeout', atEpochMs: Date.now(), ownedChildPid: child.pid });
    // Kill only this launcher's known child process tree, so a timed-out GPU
    // control cannot leave renderer/GPU helpers running into the next benchmark.
    if (process.platform === 'win32' && Number.isInteger(child.pid) && child.pid > 0) {
      const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      killer.on('error', () => child.kill()); killer.on('exit', code => { if (code) child.kill(); });
    } else child.kill('SIGKILL');
  }, options.timeoutMs);
  const [exitCode, signal] = await new Promise(resolve => child.once('close', (code, sig) => resolve([code, sig])));
  clearTimeout(timer);
  if (stdoutPending) stdoutLine(stdoutPending); if (stderrPending) stderrLine(stderrPending);
  events.push({ name: 'child-close', atEpochMs: Date.now(), exitCode, signal });
  await Promise.all([stdoutWrites, stderrWrites]); await stdout.close(); await stderr.close();
  let childResult;
  try { childResult = JSON.parse(await fs.readFile(path.join(output, 'child-result.json'), 'utf8')); }
  catch (error) { events.push({ name: 'child-result-unavailable', atEpochMs: Date.now(), error: String(error) }); }
  const classification = classifyExit({ exitCode, signal, timedOut, events, stderrLines, childResult });
  const result = { options, launcher: { nodeVersion: process.version, args, spawnError }, classification, events, child: childResult };
  await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ output, ...classification }));
  process.exitCode = classification.outcome === 'no-gpu-state-invalid-observed' ? 0 : 1;
}

export function controlPage(options) {
  const pageOptions = { mode: options.mode, seconds: options.seconds, decodeFrames: options.decodeFrames,
    hardware: options.hardware, contextLoss: options.contextLoss, cleanup: options.cleanup, finish: options.finish };
  return `<!doctype html><meta charset="utf-8"><style>html,body{margin:0}canvas{display:block}</style>
    ${['pixi', 'video'].includes(options.mode) ? '<script src="/vendor.js"></script>' : ''}
    <script type="module">
    import { parseIvf } from '/ivf.mjs';
    import { FrameDecoder } from '/decoder.mjs';
    import { createVideoOutput } from '/pixi-output.mjs';
    const o = ${JSON.stringify(pageOptions)}, delay = ms => new Promise(resolve => setTimeout(resolve, ms));
    const evidence = { mode:o.mode, webglCreated:false, webCodecsCreated:false, contextLossRequested:o.contextLoss,
      contextEvents:[], sourceFramesPublished:0, glErrors:[], completed:false };
    window.controlPartial = evidence;
    const emit = (name, data={}) => console.info('CONTROL_PAGE_EVENT '+JSON.stringify({name,atEpochMs:Date.now(),...data}));
    window.controlRun = async () => {
      let app, gl, rawTexture, graphics, sprite, pool, output, layout, streams=[], queues=[];
      const stop = () => queues.forEach(queue=>queue.stop());
      const buildOutput = () => { output=createVideoOutput(PIXI,app.renderer,layout,{separateAlpha:streams.length===2}); };
      const drawRaw = () => {
        rawTexture=gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D,rawTexture);
        gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,2,2,0,gl.RGBA,gl.UNSIGNED_BYTE,
          new Uint8Array([64,128,192,255,64,128,192,255,64,128,192,255,64,128,192,255]));
        gl.clearColor(.25,.5,.75,1);gl.clear(gl.COLOR_BUFFER_BIT);
      };
      const drawPixi = () => {
        for (let i=0;i<2;i++) { app.renderer.render(graphics,{renderTexture:pool[i],clear:true});
          sprite.texture=pool[i]; app.renderer.render(app.stage); }
      };
      const decode = async () => {
        queues.forEach(queue=>queue.start(0));
        for (let i=0;i<Math.min(o.decodeFrames,evidence.fixtureFrames);i++) {
          queues.forEach(queue=>queue.demand(i));
          const end=performance.now()+5000;
          while (!queues.every(queue=>queue.ready.has(i))) {
            const error=queues.find(queue=>queue.error)?.error;if(error)throw error;
            if(performance.now()>end)throw Error('Decode control target timed out');await delay(0);
          }
          const frames=queues.map(queue=>queue.take(i));
          try {
            if(o.mode==='video') { const presented=output.present(...frames);if(!presented)throw Error('Video control failed to publish');
              sprite.texture=presented.texture;app.renderer.render(app.stage); }
            evidence.sourceFramesPublished++;
          } finally { frames.forEach((frame,j)=>queues[j].release(frame)); }
        }
        stop();
      };
      try {
        emit('page-exercise-start',{mode:o.mode});
        if(['decode','video'].includes(o.mode)) {
          const metadata=await(await fetch('/fixture/clip.json')).json();layout=metadata.layout;evidence.fixtureFrames=metadata.frames;
          const split=metadata.schema==='amadeus.frame-video-split-experiment.v1';
          if(split && metadata.pairing!=='sourceIndex')throw Error('Unsupported fixture pairing');
          const descriptors=split?[metadata.streams.color,metadata.streams.alpha]:[metadata];
          for(const descriptor of descriptors) {
            if(!['clip.ivf','color.ivf','alpha.ivf'].includes(descriptor.video))throw Error('Unexpected fixture file');
            const ivf=parseIvf(await(await fetch('/fixture/'+descriptor.video)).arrayBuffer(),descriptor.index);
            const config={codec:descriptor.codec,codedWidth:ivf.width,codedHeight:ivf.height,colorSpace:descriptor.colorSpace,
              hardwareAcceleration:o.hardware,optimizeForLatency:true};
            const support=await VideoDecoder.isConfigSupported(config);if(!support.supported)throw Error('Unsupported decode control');
            streams.push({ivf,config});
          }
          queues=streams.map(s=>new FrameDecoder(s.ivf.frames,s.config,{capacity:4}));evidence.webCodecsCreated=true;
        }
        if(o.mode==='webgl') {
          const canvas=document.createElement('canvas');canvas.width=256;canvas.height=128;document.body.appendChild(canvas);
          gl=canvas.getContext('webgl2')||canvas.getContext('webgl');if(!gl)throw Error('WebGL unavailable');drawRaw();
        }
        if(['pixi','video'].includes(o.mode)) {
          const width=layout?.width??256,height=layout?.height??127;
          app=new PIXI.Application({width,height,resolution:1,backgroundAlpha:0,autoStart:false,antialias:false});
          document.body.appendChild(app.view);gl=app.renderer.gl;sprite=new PIXI.Sprite();app.stage.addChild(sprite);
          if(o.mode==='pixi') {
            graphics=new PIXI.Graphics().beginFill(0x4080c0).drawRect(0,0,width,height).endFill();
            pool=Array.from({length:2},()=>PIXI.RenderTexture.create({width,height,resolution:1}));drawPixi();
          } else buildOutput();
        }
        if(gl) {
          evidence.webglCreated=true;const debug=gl.getExtension('WEBGL_debug_renderer_info');
          evidence.gpu=debug?gl.getParameter(debug.UNMASKED_RENDERER_WEBGL):null;
        }
        if(queues.length)await decode();
        if(o.contextLoss) {
          const ext=gl.getExtension('WEBGL_lose_context');if(!ext)throw Error('Context-loss extension unavailable');
          const canvas=app?.view??gl.canvas;
          await new Promise((resolve,reject)=>{
            const clean=()=>{clearTimeout(timer);canvas.removeEventListener('webglcontextlost',lost);canvas.removeEventListener('webglcontextrestored',restored);};
            const lost=event=>{event.preventDefault();evidence.contextEvents.push('lost');emit('page-context-lost');
              if(!app)setTimeout(()=>ext.restoreContext(),0);};
            const restored=()=>{evidence.contextEvents.push('restored');emit('page-context-restored');clean();resolve();};
            const timer=setTimeout(()=>{clean();reject(Error('Control context restoration timed out'));},10000);
            canvas.addEventListener('webglcontextlost',lost);canvas.addEventListener('webglcontextrestored',restored);ext.loseContext();
          });
          if(o.mode==='webgl')drawRaw();
          if(o.mode==='pixi')drawPixi();
          if(o.mode==='video') {sprite.texture=PIXI.Texture.EMPTY;output.destroy();buildOutput();await decode();}
          if(gl.isContextLost())throw Error('Context remained lost');
        }
        await delay(o.seconds*1000);emit('page-exercise-end');
        if(gl)evidence.glErrors.push(gl.getError());
        evidence.completed=true;return evidence;
      } finally {
        emit('page-cleanup-start',{cleanup:o.cleanup,finish:o.finish});stop();
        evidence.decoderOwnership=queues.map(q=>q.snapshot());
        if(o.cleanup==='page') {
          if(o.finish&&gl&&!gl.isContextLost())gl.finish();
          if(sprite)sprite.texture=PIXI.Texture.EMPTY;
          output?.destroy();if(output)evidence.outputAfterDestroy=output.snapshot();
          if(pool)for(const texture of pool)texture.destroy(true);graphics?.destroy();
          if(rawTexture&&!gl.isContextLost())gl.deleteTexture(rawTexture);
          if(o.finish&&gl&&!gl.isContextLost())gl.finish();
          if(gl)evidence.glErrors.push(gl.getError());
          app?.destroy(true,{children:true,texture:false,baseTexture:false});
        }
        evidence.contextLostAfterPageCleanup=gl?.isContextLost()??null;emit('page-cleanup-end');
      }
    };
    </script>`;
}

async function childMain(options) {
  const { app, BrowserWindow, contentTracing } = await import('electron');
  const output=path.resolve(options.output), events=[];
  const emit=(name,data={})=>{const event={name,atEpochMs:Date.now(),pid:process.pid,...data};events.push(event);console.log('CONTROL_EVENT '+JSON.stringify(event));};
  const write=(name,value)=>fs.writeFile(path.join(output,name),JSON.stringify(value,null,2));
  app.setPath('userData',path.join(output,'profile'));app.commandLine.appendSwitch('disable-background-timer-throttling');
  if(options.highPerformanceGpu)app.commandLine.appendSwitch('force-high-performance-gpu');
  app.on('window-all-closed',()=>{});
  app.on('will-quit',()=>emit('app-will-quit'));
  const files=new Map([['/vendor.js',path.join(root,'render/web/vendor/pixi.min.js')],
    ...['ivf.mjs','decoder.mjs','pixi-output.mjs'].map(name=>['/'+name,path.join(root,'tools/probes/frame-video',name)])]);
  if(options.fixture)for(const name of ['clip.json','clip.ivf','color.ivf','alpha.ivf'])files.set('/fixture/'+name,path.join(path.resolve(options.fixture),name));
  const server=http.createServer(async(req,res)=>{
    try {
      const url=new URL(req.url,'http://localhost').pathname;
      if(req.method!=='GET'){res.writeHead(404).end();return;}
      if(url==='/'){res.writeHead(200,{'Content-Type':'text/html','Cache-Control':'no-store'}).end(controlPage(options));return;}
      const file=files.get(url);if(!file){res.writeHead(404).end();return;}
      const bytes=await fs.readFile(file);res.writeHead(200,{'Content-Type':file.endsWith('.json')?'application/json':/\.m?js$/.test(file)?'text/javascript':'application/octet-stream','Cache-Control':'no-store'}).end(bytes);
    } catch(error){res.writeHead(500).end();console.error(error);}
  });
  let window,tracing=false,result,failure;
  try {
    emit('child-start',{versions:process.versions,options});
    await deadline(app.whenReady(),30000,'Electron ready');
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
    window=new BrowserWindow({show:false,width:1100,height:1100,webPreferences:{nodeIntegration:false,contextIsolation:true,backgroundThrottling:false}});
    window.webContents.on('console-message',event=>{
      const text=String(event.message);
      if(text.startsWith('CONTROL_PAGE_EVENT ')){try{const data=JSON.parse(text.slice('CONTROL_PAGE_EVENT '.length));emit(data.name,{page:data});}catch(error){console.error(error);}}
      else console.log(text);
    });
    let rejectCrash;const crash=new Promise((_resolve,reject)=>{rejectCrash=reject;});crash.catch(()=>{});
    window.webContents.on('render-process-gone',(_event,details)=>rejectCrash(Error('Control renderer exited: '+details.reason)));
    if(options.trace){emit('trace-start-requested');await deadline(contentTracing.startRecording({included_categories:['gpu','media','blink.user_timing','disabled-by-default-media','disabled-by-default-gpu.service']}),10000,'Trace start');tracing=true;}
    await deadline(window.loadURL('http://127.0.0.1:'+server.address().port+'/'),30000,'Control load');
    result=await deadline(Promise.race([window.webContents.executeJavaScript('window.controlRun()'),crash]),options.timeoutMs-500,'Control exercise');
    emit('page-result-received',{completed:result.completed,webglCreated:result.webglCreated,webCodecsCreated:result.webCodecsCreated});
    if(options.gpuInfo) {
      emit('gpu-info-requested');await write('gpu-info.json',await deadline(app.getGPUInfo('complete'),10000,'GPU info'));
      emit('gpu-info-completed');
    }
    await delay(options.settleMs);
  } catch(error) {failure=String(error);console.error(error);}
  finally {
    emit('host-window-destroy-start');window?.destroy();emit('host-window-destroy-end');await delay(options.settleMs);
    if(tracing){emit('trace-stop-requested');try{await deadline(contentTracing.stopRecording(path.join(output,'trace.json')),10000,'Trace stop');emit('trace-stop-completed');}catch(error){failure??=String(error);console.error(error);}}
    server.closeAllConnections();
    if(server.listening)await deadline(new Promise(resolve=>server.close(resolve)),3000,'HTTP shutdown');
    const badGl=result?.glErrors.some(value=>value!==0);
    const childResult={completed:Boolean(result?.completed)&&!failure&&!badGl,failure,result,events,versions:process.versions,
      platform:process.platform,arch:process.arch,options,hiddenWindow:true};
    const exitCode=failure||badGl?1:0;
    emit('app-exit-requested',{method:options.exitMethod,exitCode});
    await write('child-result.json',childResult);
    // Complete queued stage writes before the deliberately immediate app.exit.
    await deadline(new Promise((resolve,reject)=>process.stdout.write('',error=>error?reject(error):resolve())),1000,'Stage output flush');
    // The Node launcher, rather than this process, classifies final native logs.
    if(options.exitMethod==='quit'){process.exitCode=exitCode;app.quit();}else app.exit(exitCode);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === here) {
  Promise.resolve().then(()=>{
    const options=parseOptions(process.argv.slice(2));
    return process.versions.electron?childMain(options):launch(options);
  }).catch(async error=>{
    console.error(error);
    if(process.versions.electron){const {app}=await import('electron');app.exit(1);}else process.exitCode=1;
  });
}
