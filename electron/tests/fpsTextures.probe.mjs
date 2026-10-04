// Isolated current-source hardware probe. Default: 60 seconds, sampling OFF.
import { app, BrowserWindow } from 'electron'
import fs from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'
import { spawn, execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createInterface } from 'node:readline'
import { parseArgs, usage, createJourney, createEvidenceSummary, installTextureProbe, installTranscodeCounter } from './textureProbe.mjs'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
let options
try { options = parseArgs(process.argv.slice(2)) }
catch (error) { console.error(error.message + '\n' + usage); app.exit(2) }
if (!options || options.help) { if (options?.help) console.log(usage); app.exit(0) }
else {
  const output = options.output ? path.resolve(options.output) : path.join(root, 'output/diagnostics/fps-textures',
    new Date().toISOString().replaceAll(':', '-') + `-${options.mode}-${options.fps}-${process.pid}`)
  await fs.mkdir(path.dirname(output), { recursive: true })
  await fs.mkdir(output) // Refuse to mix or overwrite earlier evidence.
  app.setPath('userData', path.join(output, 'profile'))
  app.on('window-all-closed', () => {})

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
  const timeout = async (promise, ms, message) => {
    let timer
    try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(Error(message)), ms) })]) }
    finally { clearTimeout(timer) }
  }
  let host, window, bridge, seq = 0, paintCount = 0, stopped = false, droppedEvents = 0
  let startedAt, phaseName = 'startup', lastDrainAt = null
  const pending = new Map(), phaseStatus = [], evidence = createEvidenceSummary()
  const metadata = { schema: 'amadeus.texture-probe.v2', options, source: 'current checkout',
    cpuProfiling: process.env.TEXTURE_PROBE_CPU_PROFILE === '1',
    versions: process.versions, platform: { os: os.platform(), release: os.release(), architecture: os.arch() },
    offscreen: true, offscreenFrameRate: 60, sourceSha256: {}, startedAtUtc: new Date().toISOString(),
    limitations: ['Offscreen Electron with capture readback; real WebView2/CEF and physical presentation need separate measurement.',
      'Legacy display misses count _showFrame attempts; changes count texture identity changes observed after each ticker callback.',
      'GPU process private memory is process memory, not texture GPU allocation. getTextureStats is recorded separately when available.',
      'Repeat phases repeat cold inputs; preload convergence is observed, not assumed. No video encoding during measurement.',
      'Scenario phases use the public work activity; companion phases measure wallpaper presentation suppression, not a separate companion window.'] }
  const writeJson = (name, value) => fs.writeFile(path.join(output, name), JSON.stringify(value, null, 2))
  const append = (name, rows) => rows.length ? fs.appendFile(path.join(output, name), rows.map(row => JSON.stringify(row)).join('\n') + '\n') : Promise.resolve()
  const failPending = error => { for (const call of pending.values()) call.reject(error); pending.clear() }
  const js = code => timeout(window.webContents.executeJavaScript(code), 15000, 'Renderer evaluation timed out')

  async function startHost() {
    const hostArgs = ['-X', 'utf8', '-u', 'tools/probes/wallpaper_memory_host.py', ...(options.scenario ? ['--scenario'] : [])]
    const python = process.env.TEXTURE_PROBE_PYTHON || path.join(root, '.venv/Scripts/python.exe')
    if (!path.isAbsolute(python)) throw Error('TEXTURE_PROBE_PYTHON must be an absolute interpreter path')
    await fs.access(python)
    host = spawn(python, hostArgs, { cwd: root, windowsHide: true,
      env: { ...process.env, GRAPHICS_PROFILE: options.profile, RENDER_MAX_FPS: String(options.fps), RENDER_MAX_RESOLUTION: '1.5',
        RENDER_TEXTURE_SAMPLING: options.sampling ? 'true' : 'false', WALLPAPER_WHEEL_FORWARD: 'false',
        AMADEUS_SCENARIO_IDLE_SECONDS: String(options.durationSeconds + 120),
        LOCALAPPDATA: path.join(output, 'host-profile'), APPDATA: path.join(output, 'host-profile'), PYTHONIOENCODING: 'utf-8' },
      stdio: ['pipe', 'pipe', 'pipe'] })
    host.stderr.on('data', data => { void fs.appendFile(path.join(output, 'host.log'), data).catch(console.error) })
    let readyResolve, readyReject
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject })
    host.once('error', error => { readyReject(error); failPending(error) })
    host.stdin.on('error', error => failPending(error))
    host.once('exit', code => {
      const error = Error(`Probe host exited (${code}); see host.log`)
      if (!bridge) readyReject(error)
      failPending(error)
    })
    createInterface({ input: host.stdout }).on('line', line => {
      let value
      try { value = JSON.parse(line) }
      catch { void fs.appendFile(path.join(output, 'host.log'), line + '\n').catch(console.error); return }
      if (value.ready) { bridge = value; readyResolve(value) }
      else if (pending.has(value.id)) {
        const call = pending.get(value.id); pending.delete(value.id)
        if (value.error) call.reject(Error(value.error)); else call.resolve(value.result)
      }
    })
    return timeout(ready, 90000, 'Probe host startup timed out; see host.log')
  }

  async function rpc(command, params = {}) {
    if (!host || host.exitCode !== null || host.stdin.destroyed) throw Error('Probe host unavailable')
    const id = ++seq
    try {
      return await timeout(new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject })
        host.stdin.write(JSON.stringify({ id, command, ...params }) + '\n')
      }), 10000, `Probe host RPC timed out: ${command}`)
    } finally { pending.delete(id) }
  }

  const snapshot = `(() => {
    const s=renderApp._sprite,a=wallpaperApp.scene.app;
    const buffers=new Set(); let cpuBytes=0;
    const textures=[...new Set(Object.values(PIXI.utils.BaseTextureCache))];
    for(const t of textures) for(const value of [...Object.values(t.resource||{}),...(t.resource?._levelBuffers||[]).map(l=>l.levelBuffer)]) {
      const b=ArrayBuffer.isView(value)?value.buffer:value instanceof ArrayBuffer?value:null;
      if(b&&!buffers.has(b)){buffers.add(b);cpuBytes+=b.byteLength;}
    }
    const gl=a.renderer.gl,ext=gl.getExtension('WEBGL_debug_renderer_info');
    let textureStats=null,textureStatsError=null;
    try { if(typeof renderApp.getTextureStats==='function') textureStats=renderApp.getTextureStats(); }
    catch(error){textureStatsError=String(error);}
    return {rawTickerMaxFps:a.ticker.maxFPS,resolution:a.renderer.resolution,dpr:devicePixelRatio,
      gpu:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null,contextLost:gl.isContextLost(),
      legacyFrames:Object.values(s._frames||{}).reduce((n,arr)=>n+arr.filter(Boolean).length,0),
      queue:s._frameLoadQueue?.size??null,activeLoads:s._activeFrameSetLoads??null,cpuBufferBytes:cpuBytes,
      glTextures:a.renderer.texture?.managedTextures?.length??null,gcCount:a.renderer.textureGC?.count??null,
      sampleFps:s._textureSampleFps??null,samplingEnabled:s._textureSamplingEnabled??null,
      label:s._currentEmotion,logical:s._frameIdx,source:s._activeFrameIdx,spriteRenderable:s.container.renderable,
      currentLabelLoadedFrames:(s._frames?.[s._currentEmotion]||[]).filter(Boolean).length,
      currentLabelTotalFrames:s._frames?.[s._currentEmotion]?.length??null,
      textureStats,textureStatsError,transcodes:window.__textureTranscodes??null};
  })()`

  async function drain() {
    const result = await js('window.__textureProbe.drain()')
    droppedEvents += result.dropped
    evidence.ingest(result.events)
    await append('events.ndjson', result.events)
    lastDrainAt = Date.now()
  }

  async function sample(stage) {
    await drain()
    const rendererPid = window.webContents.getOSProcessId()
    const processes = app.getAppMetrics().map(process => ({ pid: process.pid, type: process.type,
      cpuSeconds: process.cpu.cumulativeCPUUsage ?? null, cpuPercent: process.cpu.percentCPUUsage,
      rssBytes: process.memory.workingSetSize * 1024, privateBytes: process.memory.privateBytes * 1024,
      sharedBytes: process.memory.sharedBytes * 1024 }))
    const row = { stage, phase: phaseName, elapsedMs: Date.now() - startedAt, timeUtc: new Date().toISOString(),
      configuredMaxFps: bridge.settings.renderMaxFps, offscreenFrameRate: window.webContents.getFrameRate(),
      paints: paintCount, rendererPid, rendererProcesses: processes.filter(process => process.pid === rendererPid),
      gpuProcesses: processes.filter(process => process.type.toLowerCase().includes('gpu')),
      otherProcesses: processes.filter(process => process.pid !== rendererPid && !process.type.toLowerCase().includes('gpu')),
      host: await rpc('sample'), render: await js(snapshot) }
    await append('samples.ndjson', [row])
    console.log(JSON.stringify({ stage, elapsedSeconds: Math.round(row.elapsedMs / 1000), frames: row.render.legacyFrames,
      rendererPrivateMiB: row.rendererProcesses.map(process => Math.round(process.privateBytes / 1048576)),
      gpuPrivateMiB: row.gpuProcesses.map(process => Math.round(process.privateBytes / 1048576)), output }))
  }

  async function action(item) {
    phaseName = item.phase
    await js(`window.__textureProbe.setPhase(${JSON.stringify(phaseName)})`)
    const row = { ...item, actualAtSeconds: (Date.now() - startedAt) / 1000 }
    if (item.command === 'trigger') {
      const exists = await js(`Boolean(renderApp._spriteforgeRuntime.labelToIds[${JSON.stringify(item.label)}]?.length)`)
      if (!exists) {
        row.status = 'unavailable'; row.reason = 'trigger label missing from loaded graph'
        phaseStatus.push({ phase: phaseName, status: row.status, label: item.label, reason: row.reason })
      }
    }
    if (!row.status) { await rpc(item.command, item); row.status = 'requested' }
    await append('journey.ndjson', [row])
  }

  async function optionalPhase(name, available, reason, operation) {
    if (!available) { phaseStatus.push({ phase: name, status: 'unavailable', reason }); return }
    phaseName = name
    await js(`window.__textureProbe.setPhase(${JSON.stringify(name)})`)
    const result = await operation()
    phaseStatus.push({ phase: name, status: 'exercised', ...result })
    await sample(name)
    if (result.status === 'failed') throw Error(`${name}: ${result.reason || 'validation failed'}`)
  }

  async function run() {
    let failure
    const sourceHash = async source => {
      try { return createHash('sha256').update(await fs.readFile(path.join(root, source))).digest('hex') }
      catch (error) { if (error.code === 'ENOENT') return null; throw error }
    }
    try {
      metadata.revision = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, windowsHide: true }).toString().trim()
      metadata.branch = execFileSync('git', ['branch', '--show-current'], { cwd: root, windowsHide: true }).toString().trim()
      for (const source of ['render/web/renderer.js', 'render/web/render_budget.js', 'render/web/frame_store.js',
        'render/web/frame_texture_backend.js', 'render/web/wallpaper_scene.js',
        'render/web/wallpaper_engine_bridge.js', 'render/web/wallpaper_engine.html', 'render/web/vendor/pixi.min.js',
        'render/web/vendor/pixi-basis-ktx2.global.js', 'render/server.py', 'render/spriteforge_animator.py',
        'wallpaper/wallpaper_engine_bridge.py', 'wallpaper/scene_assets.py', 'tools/probes/wallpaper_memory_host.py',
        'electron/tests/fpsTextures.probe.mjs', 'electron/tests/textureProbe.mjs']) {
        metadata.sourceSha256[source] = await sourceHash(source)
      }
      await startHost()
      metadata.host = bridge
      await writeJson('metadata.json', metadata)
      if (!bridge.characterAvailable) throw Error('Optional SpriteForge character pack unavailable; see metadata.json')
      window = new BrowserWindow({ width: 960, height: 600, show: false, frame: false, transparent: true, backgroundColor: '#00000000',
        webPreferences: { offscreen: true, backgroundThrottling: false, sandbox: true, contextIsolation: true, nodeIntegration: false } })
      window.webContents.setFrameRate(60)
      window.webContents.setAudioMuted(true)
      window.webContents.on('paint', () => { paintCount++ })
      window.webContents.on('console-message', event => { void fs.appendFile(path.join(output, 'renderer.log'), event.message + '\n').catch(console.error) })
      // Materialize the offscreen renderer before issuing Page commands;
      // otherwise a brand-new hidden window may never acknowledge Page.enable.
      await timeout(window.loadURL('about:blank'), 10000, 'Probe blank page initialization timed out')
      window.webContents.debugger.attach('1.3')
      await timeout(window.webContents.debugger.sendCommand('Page.enable'), 10000, 'Probe Page.enable timed out')
      await timeout(window.webContents.debugger.sendCommand('Page.addScriptToEvaluateOnNewDocument', {
        source: `(${installTranscodeCounter.toString()})()` }), 10000, 'Probe script registration timed out')
      // Never discover or contact the user's backend, even if a renderer regresses.
      const allowedPorts = new Set([String(bridge.assetPort), String(bridge.bridgePort)])
      window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
        const url = new URL(details.url)
        const allowed = ['blob:', 'data:', 'about:'].includes(url.protocol) ||
          (url.hostname === '127.0.0.1' && allowedPorts.has(url.port) && ['http:', 'ws:'].includes(url.protocol))
        callback({ cancel: !allowed })
      })
      await timeout(window.loadURL(bridge.url), 30000, 'Renderer navigation timed out')
      const deadline = Date.now() + 30000
      while (!await js('Boolean(window.renderApp?._spriteforgeRuntime?.rootNodeId && window.wallpaperApp?.scene?.app)')) {
        if (Date.now() > deadline) throw Error('Renderer runtime not ready; see renderer.log')
        await sleep(100)
      }
      metadata.instrumentation = await js(`(${installTextureProbe.toString()})(${options.seed})`)
      metadata.instrumentation.transcodes = await js('window.__textureTranscodes?.installed===true')
      if (!metadata.instrumentation.transcodes) throw Error('Common transcoder instrumentation was not installed')
      if (metadata.cpuProfiling) {
        await window.webContents.debugger.sendCommand('Profiler.enable')
        await window.webContents.debugger.sendCommand('Profiler.setSamplingInterval', { interval: 1000 })
        await window.webContents.debugger.sendCommand('Profiler.start')
      }
      startedAt = Date.now()
      const journey = createJourney(options)
      metadata.journey = journey
      const triggerLabels = [...new Set(journey.filter(item => item.command === 'trigger').map(item => item.label))]
      metadata.triggerAvailability = await js(`Object.fromEntries(${JSON.stringify(triggerLabels)}.map(label=>
        [label,Boolean(renderApp._spriteforgeRuntime.labelToIds[label]?.length)]))`)
      await writeJson('metadata.json', metadata)
      let cursor = 0, nextSample = 0
      while (Date.now() - startedAt < options.durationSeconds * 1000) {
        const elapsed = (Date.now() - startedAt) / 1000
        while (cursor < journey.length && journey[cursor].at <= elapsed) await action(journey[cursor++])
        if (elapsed >= nextSample) { await sample('journey'); nextSample += options.sampleSeconds }
        await sleep(50)
      }
      await sample('journey-complete')
      if (metadata.cpuProfiling) {
        const { profile } = await window.webContents.debugger.sendCommand('Profiler.stop')
        await writeJson('cpu-profile.cpuprofile', profile)
        window.webContents.debugger.detach()
      }
      phaseStatus.push({ phase: 'journey', status: 'exercised', requestedSeconds: options.durationSeconds,
        actualSeconds: (Date.now() - startedAt) / 1000 })
      if (options.companion) await optionalPhase('companion', await js("typeof wallpaperApp.setCompanionActive==='function'"), 'public companion API unavailable', async () => {
        const before = await js('renderApp._sprite.container.renderable')
        await rpc('companion', { active: true }); await sleep(3000); await sample('companion-suppressed')
        const suppressed = await js('renderApp._sprite.container.renderable===false')
        await rpc('companion', { active: false }); await sleep(3000)
        const restored = await js('renderApp._sprite.container.renderable') === before
        return { scope: 'wallpaper layer suppression and restoration', suppressed, restored,
          status: suppressed && restored ? 'exercised' : 'requested', activation: suppressed && restored ? 'observed' : 'unverified' }
      })
      if (options.scenario) await optionalPhase('scenario', bridge.scenario.workActivityAvailable, bridge.scenario.reason || 'work activity node unavailable', async () => {
        await rpc('scenario', { active: true })
        for (let i = 0; i < 5; i++) { await sleep(2000); await sample('scenario-active') }
        await rpc('scenario', { active: false }); await sleep(2000)
        return { scope: 'public work activity; inspect renderer.log for activation evidence', status: 'requested', activation: 'unverified' }
      })
      if (options.contextLoss) await optionalPhase('context-loss', await js("Boolean(wallpaperApp.scene.app.renderer.gl.getExtension('WEBGL_lose_context'))"), 'WEBGL_lose_context unavailable', async () => {
        // Freeze the same source image. A restored event and a valid Texture
        // object can still hide failed compressed uploads (an opaque black quad).
        await js('renderApp.holdSpriteFrame(renderApp._sprite._activeFrameIdx)')
        const texturePixels = `(() => {
          const app=wallpaperApp.scene.app,texture=renderApp._sprite.sprite.texture;
          const target=new PIXI.Sprite(texture);
          let pixels;
          try{pixels=app.renderer.extract.pixels(target);}finally{target.destroy({texture:false,baseTexture:false});}
          let hash=2166136261,rgbSum=0,alphaSum=0;
          for(let i=0;i<pixels.length;i++){hash=Math.imul(hash^pixels[i],16777619);if(i%4===3)alphaSum+=pixels[i];else rgbSum+=pixels[i];}
          const gl=app.renderer.gl,errors=[];
          for(let i=0;i<16;i++){const error=gl.getError();if(error===gl.NO_ERROR)break;errors.push(error);}
          return {width:texture.width,height:texture.height,hash:(hash>>>0).toString(16),rgbSum,alphaSum,glErrors:errors};
        })()`
        const beforePixels = await js(texturePixels)
        await js(`new Promise((resolve,reject)=>{
          const canvas=wallpaperApp.scene.app.view,ext=wallpaperApp.scene.app.renderer.gl.getExtension('WEBGL_lose_context');
          const timer=setTimeout(()=>{canvas.removeEventListener('webglcontextrestored',done);reject(Error('Context restoration timed out'))},10000);
          const done=()=>{clearTimeout(timer);canvas.removeEventListener('webglcontextrestored',done);resolve(true)};
          // Bundled Pixi requests restoration from its loss handler. A second
          // restoreContext here produces INVALID_OPERATION after it recovers.
          canvas.addEventListener('webglcontextrestored',done);ext.loseContext();
        })`)
        await sleep(3000)
        const afterPixels = await js(texturePixels)
        const restored = await js('!wallpaperApp.scene.app.renderer.gl.isContextLost()')
        await js('renderApp.clearSpriteHold()')
        const pixelMatch = beforePixels.hash === afterPixels.hash && beforePixels.rgbSum > 0
          && beforePixels.width === afterPixels.width && beforePixels.height === afterPixels.height
        const passed = restored && pixelMatch && !beforePixels.glErrors.length && !afterPixels.glErrors.length
        return { status: passed ? 'exercised' : 'failed', restored, pixelMatch, beforePixels, afterPixels,
          reason: passed ? null : 'Held texture pixels or GL errors differ after context restoration' }
      })
      await js('window.__textureProbe.stop()'); stopped = true
      await drain()
      await fs.writeFile(path.join(output, 'frame.png'), (await window.webContents.capturePage()).toPNG())
      if (droppedEvents) throw Error(`Raw evidence incomplete: ${droppedEvents} browser events dropped`)
    } catch (error) { failure = error }
    finally {
      if (window && !window.isDestroyed() && startedAt && !stopped) {
        try { await js('window.__textureProbe.stop()'); await drain() } catch (error) { metadata.cleanupError = error.message }
      }
      window?.destroy()
      if (host && host.exitCode === null) {
        host.stdin.end(JSON.stringify({ command: 'stop' }) + '\n')
        await Promise.race([new Promise(resolve => host.once('exit', resolve)), sleep(5000)])
        if (host.exitCode === null) host.kill()
      }
      const sourcesChanged = []
      for (const [source, hash] of Object.entries(metadata.sourceSha256)) {
        const current = await sourceHash(source)
        if (current !== hash) sourcesChanged.push(source)
      }
      if (sourcesChanged.length && !failure) failure = Error('Source changed during run; comparison invalid')
      await writeJson('summary.json', { complete: !failure, error: failure?.message ?? null, droppedEvents,
        phaseStatus, phases: evidence.finish(), lastDrainAtUtc: lastDrainAt ? new Date(lastDrainAt).toISOString() : null,
        sourceChangedDuringRun: sourcesChanged })
      metadata.endedAtUtc = new Date().toISOString()
      await writeJson('metadata.json', metadata)
    }
    if (failure) throw failure
    console.log(JSON.stringify({ complete: true, output }))
  }
  app.whenReady().then(run).then(() => app.exit(0), error => { console.error(error); app.exit(1) })
}
