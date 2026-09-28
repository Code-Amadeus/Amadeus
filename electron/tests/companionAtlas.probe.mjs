// node_modules/electron/dist/electron.exe tests/companionAtlas.probe.mjs VARIANT ASSETS [RUN]
// Each invocation is one isolated hardware-accelerated offscreen process.
import { app, BrowserWindow, ipcMain, screen } from 'electron'
import fs from 'node:fs/promises'
import path from 'node:path'
import http from 'node:http'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { readCompanionPortraits } from '../dist/main/companionPortraits.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const variant = process.argv[2]
assert.ok(['baseline', 'matched132', 'full132', 'full264', 'full132cpu', 'full264cpu', 'candidate132cpu', 'candidate132bcpu'].includes(variant))
const assetRoot = path.resolve(process.argv[3])
const variantManifest = variant === 'baseline' ? null : JSON.parse(await fs.readFile(path.join(assetRoot, variant.replace('cpu',''), 'manifest.json'), 'utf8'))
const native = process.argv[5] === 'native'
const output = path.join(assetRoot, '../runs', `${new Date().toISOString().replaceAll(':', '-')}-${variant}-${process.argv[4] || '1'}`)
await fs.mkdir(output, { recursive: true })
app.setPath('userData', path.join(output, 'profile'))
app.on('window-all-closed', () => {})
// File-backed diagnostics never depend on the launcher's console lifetime.
const log = data => fs.appendFile(path.join(output, 'progress.jsonl'), JSON.stringify(data) + '\n')
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const samples = [], clients = new Set(), errors = []
let window, paints = 0, phase = 'startup'
const current = { emotion: 'normal', speaking: false }
const subtitle = '先看看这里的线索，我们慢慢来。'
const publish = call => { for (const client of clients) client.write(`data: ${JSON.stringify(call)}\n\n`) }

const files = new Map()
for (const file of ['companion_panel.html','companion_panel.css','companion_panel.js','companion_presentation.js','companion_atlas.js']) {
  files.set('/' + file, path.join(root, 'render/web', file))
}
for (const file of ['companionAtlasPanel.js','companionAtlasPlayer.js']) {
  files.set('/' + file, path.join(root, 'electron/tests/fixtures', file))
}
const mime = file => ({ '.js':'application/javascript', '.html':'text/html; charset=utf-8',
  '.css':'text/css', '.webp':'image/webp', '.json':'application/json' }[path.extname(file)] || 'application/octet-stream')
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1')
    if (url.pathname === '/wallpaper/events') {
      response.writeHead(200, { 'Content-Type':'text/event-stream', 'Cache-Control':'no-cache' })
      clients.add(response)
      for (const call of [{method:'setSubtitle',args:[subtitle]}, {method:'setEmotion',args:[current.emotion]}, {method:'setSpeaking',args:[current.speaking]}]) {
        response.write(`data: ${JSON.stringify(call)}\n\n`)
      }
      request.on('close', () => clients.delete(response))
      return
    }
    if (url.pathname === '/companion_panel.html') {
      let html = await fs.readFile(files.get(url.pathname), 'utf8')
      if (variant !== 'baseline') html = html.replace('<script src="companion_panel.js"></script>',
        '<script src="companionAtlasPlayer.js"></script><script src="companionAtlasPanel.js"></script>')
      response.setHeader('Content-Type', 'text/html; charset=utf-8')
      response.end(html)
      return
    }
    let file = files.get(url.pathname)
    if (url.pathname.startsWith('/assets/')) {
      const candidate = path.resolve(assetRoot, decodeURIComponent(url.pathname.slice(8)))
      if (!candidate.startsWith(assetRoot + path.sep)) throw Error('outside experiment assets')
      file = candidate
    }
    if (!file) { response.writeHead(404); response.end(); return }
    response.setHeader('Content-Type', mime(file))
    // Do not let Chromium's HTTP cache obscure explicit application cache behavior.
    response.setHeader('Cache-Control', 'no-store')
    response.end(await fs.readFile(file))
  } catch (error) { errors.push(String(error)); response.writeHead(500); response.end() }
})

async function until(code, timeout=15000) {
  const end=Date.now()+timeout
  while(Date.now()<end) {
    const error=await window.webContents.executeJavaScript('window.probeError || null')
    if(error) throw Error(error)
    if(await window.webContents.executeJavaScript(code)) return
    await sleep(40)
  }
  throw Error('timeout: '+code)
}

async function sample() {
  const processes = app.getAppMetrics().map(p => ({ pid:p.pid, type:p.type, memoryKiB:p.memory, cpu:p.cpu }))
  const state = await window.webContents.executeJavaScript(`({
    atlas: window.atlasPlayer ? (()=>{const v=atlasPlayer.snapshot(); delete v.events; return v})() : null,
    baselineChanges: window.baselineEvents?.length || 0,
    error: window.probeError || null
  })`)
  samples.push({phase, time:Date.now(), paints, processes, rendererPid:window.webContents.getOSProcessId(), state})
  assert.equal(state.error, null)
  if(state.atlas) assert.ok(state.atlas.residentBytes <= (variant.startsWith('full264')?48:16)*1024*1024 && state.atlas.atlasCount<=2)
  await fs.writeFile(path.join(output, 'samples.json'), JSON.stringify(samples, null, 2))
}

async function measure(name, seconds) {
  phase=name
  await log({phase, at:Date.now()})
  for(let i=0;i<seconds;i++){await sleep(1000);await sample()}
}

async function setState(emotion, speaking) {
  Object.assign(current,{emotion,speaking})
  publish({method:'setEmotion',args:[emotion]})
  publish({method:'setSpeaking',args:[speaking]})
  if(variant!=='baseline') await until(`atlasPlayer.active?.url === ${JSON.stringify(variantManifest.emotions[emotion][speaking?'speaking':'idle'].url)}`)
}

async function run() {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  const port=server.address().port
  ipcMain.handle('companion.portraits',()=>readCompanionPortraits(path.resolve(root,'../visual novel player/out/vn_portrait_cache')))
  for(const name of ['connected','dock','close']) ipcMain.handle('companion.'+name,()=>true)
  const area=screen.getPrimaryDisplay().workArea
  window=new BrowserWindow({width:470,height:250,show:false,frame:false,transparent:true,backgroundColor:'#00000000',
    x:area.x+area.width-486,y:area.y+64,alwaysOnTop:native,skipTaskbar:true,focusable:false,
    webPreferences:{offscreen:!native,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false,
      preload:path.join(root,'electron/dist/preload/companion.cjs')}})
  if(!native){window.webContents.setFrameRate(60);window.webContents.on('paint',()=>{paints++})}
  window.webContents.on('render-process-gone',(_,details)=>errors.push(JSON.stringify(details)))
  window.webContents.on('console-message',event=>{
    if(event.level==='error') errors.push(event.message)
  })
  const started=performance.now()
  await window.loadURL(`http://127.0.0.1:${port}/companion_panel.html?variant=${variant}&bridgePort=${port}`)
  if(native) {
    await window.webContents.executeJavaScript(`document.querySelector('.brand').textContent='EXPERIMENT · ${variant}';window.probeRafCount=0;function probeRaf(){probeRafCount++;requestAnimationFrame(probeRaf)}requestAnimationFrame(probeRaf)`)
    window.showInactive()
  }
  await until(variant==='baseline'?'document.querySelector("#portrait").naturalWidth > 0':'Boolean(window.probeReady && atlasPlayer.active)')
  const startupMs=performance.now()-started
  if(variant==='baseline') await window.webContents.executeJavaScript(`(() => {
    window.baselineEvents=[];
    const img=document.querySelector('#portrait'), ids=new Map();
    new MutationObserver(()=>{
      if(!ids.has(img.src)) ids.set(img.src,ids.size);
      baselineEvents.push({t:performance.now(),id:ids.get(img.src)});
    }).observe(img,{attributes:true,attributeFilter:['src']});
  })()`)
  const sourceHashes={}
  for(const file of ['render/web/companion_panel.js','electron/dist/main/companionPortraits.js',
    'electron/tests/fixtures/companionAtlasPlayer.js','electron/tests/fixtures/companionAtlasPanel.js']) {
    sourceHashes[file]=createHash('sha256').update(await fs.readFile(path.join(root,file))).digest('hex')
  }
  await fs.writeFile(path.join(output,'metadata.json'),JSON.stringify({variant,startupMs,versions:process.versions,
    sourceHashes,gpu:await app.getGPUInfo('basic'), features:app.getGPUFeatureStatus(),offscreen:!native,
    scope:native?'Visible 470x250 card, no game/LLM/wallpaper. Common rAF instrumentation.':'Matched 470x250 accelerated offscreen cards; actual paint delivery, common readback overhead. No game/LLM/wallpaper running.',
    assetRoot},null,2))
  app.getAppMetrics() // Discard first CPU interval, which is reported as zero.
  await measure('idle',5)
  await setState('normal',true)
  await measure('normal-speaking',8)
  await fs.writeFile(path.join(output,'normal.png'),(await window.webContents.capturePage()).toPNG())
  await setState('sided_thinking',true)
  await measure('thinking-speaking',8)
  await fs.writeFile(path.join(output,'thinking.png'),(await window.webContents.capturePage()).toPNG())
  await setState('blush',true)
  await measure('long-speaking',6)
  // Revisit every expression to expose unbounded retained decoded resources.
  for(let cycle=0;cycle<3;cycle++) {
    for(const emotion of ['happy','sad','angry','sided_surprised','normal','sided_thinking','blush']){
      await setState(emotion,true);await sleep(120)
    }
    await measure(`churn-${cycle+1}`,2)
  }
  await setState('normal',false)
  await measure('standby',4)
  if(variant!=='baseline') {
    await window.webContents.executeJavaScript('probePause(true)')
    const before=await window.webContents.executeJavaScript('atlasPlayer.draws')
    await measure('paused',3)
    assert.equal(await window.webContents.executeJavaScript('atlasPlayer.draws'),before)
    // Superseded asynchronous decodes must not overwrite the final requested state.
    await window.webContents.executeJavaScript(`Promise.all(['angry','happy','sad','normal'].map(e=>atlasPlayer.select(e,true)))`)
    assert.equal(await window.webContents.executeJavaScript('atlasPlayer.active.url'),'normal/speaking.webp')
    await window.webContents.executeJavaScript('atlasPlayer.dispose()')
    assert.equal(await window.webContents.executeJavaScript('atlasPlayer.residentBytes'),0)
  }
  await fs.writeFile(path.join(output,'frame-events.json'),JSON.stringify(await window.webContents.executeJavaScript(
    'window.atlasPlayer ? atlasPlayer.events : baselineEvents')))
  if(native) assert.ok(await window.webContents.executeJavaScript('probeRafCount > 100'),'native renderer did not advance')
  else assert.ok(paints>100,'offscreen rendering was throttled')
  assert.deepEqual(errors,[])
  await log({complete:true,paints,startupMs})
}

app.whenReady().then(run).then(async()=>{
  window?.destroy()
  for(const client of clients)client.end()
  server.closeAllConnections();server.close()
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify({ok:true,errors}))
  app.exit(0)
},async error=>{
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify({ok:false,error:String(error.stack),errors}))
  window?.destroy();for(const client of clients)client.end();server.closeAllConnections();server.close();app.exit(1)
})
