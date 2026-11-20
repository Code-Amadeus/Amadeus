// Run with Electron after `npm run build`. The test-owned card is shown inactive
// for compositor captures; no backend, model, microphone or audio is started.
import { app, BrowserWindow, nativeImage, screen } from 'electron'
import assert from 'node:assert/strict'
import http from 'node:http'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { CompanionPanel } from '../dist/main/companionPanel.js'
import { clampPanel as clampToArea } from '../dist/main/companionPanelLayout.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const output = path.resolve(process.env.AMADEUS_COMPANION_SMOKE_OUTPUT || path.join(root, 'output/diagnostics/companion-panel'))
await fs.mkdir(output, { recursive: true })
// The supported smoke has no sibling workspace or optional character-pack prerequisite.
const portraitCache = path.join(output, 'portrait-fixture')
await fs.mkdir(portraitCache, { recursive: true })
const pixels = Buffer.alloc(132 * 132 * 4)
for (let y = 0; y < 132; y++) for (let x = 0; x < 132; x++) {
  const i = (y * 132 + x) * 4
  const face = (x - 66) ** 2 + (y - 58) ** 2 < 36 ** 2
  pixels.set(face ? [174, 191, 112, 255] : [36, 30, 7, 255], i)
}
await fs.writeFile(path.join(portraitCache, 'portrait.png'), nativeImage.createFromBitmap(pixels, { width: 132, height: 132 }).toPNG())
await fs.writeFile(path.join(portraitCache, 'manifest.json'), JSON.stringify({ emotions: {
  normal: { idle: ['portrait.png'], speaking: ['portrait.png'] },
} }))
app.setPath('userData', path.join(output, 'electron-profile'))
app.disableHardwareAcceleration()
const nativeShowInactive = BrowserWindow.prototype.showInactive
BrowserWindow.prototype.showInactive = function () {
  this.webContents.setBackgroundThrottling(false)
  nativeShowInactive.call(this)
}
const clients = new Set()
const visibility = []
const inputActions = []
const inputState = { supports_images: true, watching: false, voice: { active: false, source: '' }, wake: { running: true } }
let inputActionError = '', holdInput = false, releaseInput = null
const token = 'companion-smoke-local-only'
const seed = { method: 'setSubtitle', args: ['时钟停在九点十五分。\n先看看笔记，也许这两个线索能对上。'] }
function publish(call) { for (const response of clients) response.write(`data: ${JSON.stringify(call)}\n\n`) }
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1')
  if (url.pathname === '/slice-smoke.html') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8')
    response.end('<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'self\'; style-src \'self\' \'unsafe-inline\'; img-src \'self\' data:"><title>Slice test</title><body><script src="/render/web/crt_canvas_surface.js"></script></body>')
  } else if (url.pathname === '/wallpaper/events') {
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
    response.write(`data: ${JSON.stringify(seed)}\n\n`)
    clients.add(response)
    request.on('close', () => clients.delete(response))
  } else if (url.pathname === '/wallpaper/bridge-info') {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ bridgeToken: token }))
  } else if (url.pathname === '/wallpaper/chat-action') {
    assert.equal(request.headers['x-amadeus-bridge-token'], token)
    let body = ''; for await (const data of request) body += data
    const { action } = JSON.parse(body)
    response.setHeader('Content-Type', 'application/json')
    if (action === 'status') response.end(JSON.stringify({ ok: true, ...inputState }))
    else {
      inputActions.push(action)
      if (holdInput) await new Promise(resolve => { releaseInput = resolve })
      if (inputActionError) response.end(JSON.stringify({ ok: false, error: inputActionError }))
      else {
        if (action === 'voice_start') inputState.voice = { active: true, source: 'wake' }
        else if (action === 'voice_stop') inputState.voice = { active: false, source: '' }
        else if (action === 'vision_toggle') inputState.watching = !inputState.watching
        else throw Error('Unexpected input action: ' + action)
        response.end('{"ok":true}')
      }
    }
  } else if (url.pathname === '/wallpaper/canvas-action') {
    assert.equal(request.headers['x-amadeus-bridge-token'], token)
    let body = ''; for await (const data of request) body += data
    const value = JSON.parse(body)
    assert.equal(value.target, 'presentation'); assert.equal(value.action, 'companion')
    visibility.push(value.active)
    response.setHeader('Content-Type', 'application/json'); response.end('{"ok":true}')
  } else {
    const name = path.basename(url.pathname)
    if (!['companion_panel.html', 'companion_panel.css', 'companion_panel.js', 'companion_presentation.js', 'companion_atlas.js', 'crt_canvas_surface.js'].includes(name)) {
      response.writeHead(404); response.end(); return
    }
    response.setHeader('Content-Type', name.endsWith('.html') ? 'text/html; charset=utf-8' : name.endsWith('.css') ? 'text/css' : 'application/javascript')
    response.end(await fs.readFile(path.join(root, 'render/web', name)))
  }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port
async function until(check, message) {
  const end = Date.now() + 8000
  while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 50)) }
  throw new Error(message)
}
let panelHost
async function captureCard(panel, name) {
  // capturePage can return a previous frame for transparent windows. Subscribe
  // to the next actual compositor frame after the DOM assertions instead.
  const frame = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { panel.webContents.endFrameSubscription(); reject(Error('card did not produce a compositor frame')) }, 8000)
    panel.webContents.beginFrameSubscription(false, image => {
      clearTimeout(timer); panel.webContents.endFrameSubscription(); resolve(image)
    })
  })
  const { width, height } = frame.getSize(), pixels = frame.toBitmap()
  assert.equal(pixels[3], 0, 'outer corner must remain transparent')
  const alpha = pixels[(Math.floor(height / 2) * width + Math.floor(width / 2)) * 4 + 3]
  assert.ok(Math.abs(alpha - 235) <= 1, 'whole-card alpha must match VN opacity')
  await fs.writeFile(path.join(output, name), frame.toPNG())
}
async function run() {
try {
  const area = screen.getPrimaryDisplay().workArea
  await fs.writeFile(path.join(output, 'companion-position.json'), JSON.stringify({ x: area.x + 30, y: area.y + 30, width: 470, height: 250 }))
  const game = new BrowserWindow({ ...area, minWidth: 720, minHeight: 520, show: false })
  const original = game.getBounds()
  const slice = new BrowserWindow({ show: false, webPreferences: { preload: path.join(root, 'electron/dist/preload/slice.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false } })
  slice.webContents.on('console-message', event => { console.error('[slice test]', event.message) })
  panelHost = new CompanionPanel({ userDataDir: output,
    preload: path.join(root, 'electron/dist/preload/companion.cjs'),
    portraitCacheDir: process.env.AMADEUS_COMPANION_PORTRAIT_CACHE || portraitCache,
    bridge: () => ({ assetPort: port, bridgePort: port, assetVersion: 'smoke' }),
    target: id => id === 'work-test' ? game : null, slice: () => slice.webContents,
  })
  await slice.loadURL(`http://127.0.0.1:${port}/slice-smoke.html`)
  const setupError = await slice.webContents.executeJavaScript(`(() => { try {
    window.testSurface = window.createCrtCanvasSurface();
    window.testSurface.layout({x:0,y:0,width:800,height:600});
    window.testSurface.setPayload({taskDock:{revision:'1',selectedWorkItemId:'work-test',items:[{id:'work-test',attemptId:'attempt-test',workspacePath:'F:/test'}]}});
    if (!document.querySelector('[data-action="companion"]')) window.testSurface.toggle();
    return ''; } catch (error) { return error.stack; } })()`)
  assert.equal(setupError, '')
  assert.equal(await slice.webContents.executeJavaScript(`document.querySelector('[data-action="companion"]').previousElementSibling.textContent`), 'W')
  await slice.webContents.executeJavaScript(`document.querySelector('[data-action="companion"]').click()`)
  await until(() => slice.webContents.executeJavaScript(`document.querySelector('[data-action="companion"]').getAttribute('aria-pressed') === 'true'`), 'Slice toggle did not open the panel')
  const panel = BrowserWindow.getAllWindows().find(window => window !== game && window !== slice)
  assert.ok(panel)
  panel.webContents.on('console-message', event => { console.error('[card test]', event.message) })
  await until(() => visibility.at(-1) === true, 'original display was not suppressed')
  await until(() => panel.webContents.executeJavaScript('document.querySelector("#portrait").naturalWidth > 0'), 'fixture portrait did not load')
  await until(() => panel.getBounds().height === 226, 'saved height was not fitted to the short caption')
  const layout = await panel.webContents.executeJavaScript(`(() => {
    const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
    return { portrait: rect('.portrait'), caption: rect('#caption'), opacity: getComputedStyle(document.querySelector('.companion-card')).opacity,
      viewportWidth: innerWidth, font: getComputedStyle(document.querySelector('#caption')).fontSize, controls: getComputedStyle(document.querySelector('.controls')).visibility };
  })()`)
  assert.deepEqual(layout.portrait, [23, 58, 132, 132])
  assert.deepEqual(layout.caption.slice(0, 2), [177, 84])
  assert.equal(layout.caption[2], layout.viewportWidth - 177 - 27)
  assert.equal(layout.opacity, '0.92')
  assert.equal(layout.font, '14px')
  assert.equal(layout.controls, 'hidden')
  // Visibility boundary fixture; hidden diagnostic windows disable Chromium
  // throttling, which otherwise suspends executeJavaScript on Windows.
  const hiddenScan = await panel.webContents.executeJavaScript(`(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
    const paused = getComputedStyle(document.querySelector('.scan-sweep')).animationPlayState;
    delete document.hidden; document.dispatchEvent(new Event('visibilitychange'));
    return paused;
  })()`)
  assert.equal(hiddenScan, 'paused')
  const foreign = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: true, contextIsolation: false } })
  await foreign.loadURL('about:blank')
  assert.equal(await foreign.webContents.executeJavaScript(`require('electron').ipcRenderer.invoke('companion.fit-content', 500)`), false)
  assert.equal((await foreign.webContents.executeJavaScript(`require('electron').ipcRenderer.invoke('companion.input', 'voice_start')`)).ok, false)
  foreign.destroy()
  assert.equal((await panel.webContents.executeJavaScript(`window.companion.input('new_chat')`)).ok, false)
  assert.equal(await panel.webContents.executeJavaScript('window.companion.fitContent(Infinity)'), false)
  assert.equal(await panel.webContents.executeJavaScript('window.companion.fitContent(100)'), false)
  const a = game.getBounds(), b = panel.getBounds()
  assert.equal(a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y, false)
  publish({ method: 'triggerCharacterIntent', args: ['trans_smile'] })
  publish({ method: 'setSpeaking', args: [true] })
  await until(() => panel.webContents.executeJavaScript('document.body.classList.contains("speaking")'), 'speaking did not reach card')
  await new Promise(resolve => setTimeout(resolve, 400))
  await until(() => panel.webContents.executeJavaScript(`!document.querySelector('#portrait').hidden && document.querySelector('#fallback').hidden`), 'loaded portrait did not replace the placeholder')
  panel.focus() // Keyboard focus requires an active native window, not only a DOM activeElement.
  await panel.webContents.executeJavaScript(`document.querySelector('#card').focus(); document.querySelector('#voice').focus()`)
  assert.equal(await panel.webContents.executeJavaScript(`getComputedStyle(document.querySelector('.controls')).visibility`), 'visible')
  await captureCard(panel, 'companion-controls.png')
  await panel.webContents.executeJavaScript(`document.activeElement.blur(); new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`)
  await captureCard(panel, 'companion-card.png')
  await until(() => panel.webContents.executeJavaScript(`!document.querySelector('#voice').disabled`), 'Host input status did not enable the microphone')
  assert.equal(await panel.webContents.executeJavaScript(`document.querySelector('#dock') === null && document.querySelector('#motion') === null && window.companion.dock === undefined`), true)
  holdInput = true
  const actionsBefore = inputActions.length
  await panel.webContents.executeJavaScript(`document.querySelector('#voice').click()`)
  await until(() => inputActions.at(-1) === 'voice_start' && releaseInput, 'voice start did not reach the Host')
  assert.equal(await panel.webContents.executeJavaScript(`document.querySelector('#voice').disabled && document.querySelector('#vision').disabled`), true)
  await panel.webContents.executeJavaScript(`document.querySelector('#voice').click(); document.querySelector('#vision').click()`)
  holdInput = false; releaseInput(); releaseInput = null
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#voice').getAttribute('aria-pressed') === 'true'`), 'voice state did not follow Host completion')
  assert.equal(inputActions.length, actionsBefore + 1, 'pending clicks must not duplicate an input action')
  await panel.webContents.executeJavaScript(`document.querySelector('#voice').click()`)
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#voice').getAttribute('aria-pressed') === 'false' && !document.querySelector('#voice').disabled`), 'voice stop did not follow Host completion')
  await panel.webContents.executeJavaScript(`document.querySelector('#vision').click()`)
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#vision').getAttribute('aria-pressed') === 'true'`), 'vision toggle did not follow Host completion')
  inputActionError = 'fixture_denied'
  await panel.webContents.executeJavaScript(`document.querySelector('#vision').click()`)
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#vision').title.includes('fixture_denied')`), 'input rejection was not shown')
  assert.equal(await panel.webContents.executeJavaScript(`document.querySelector('#vision').getAttribute('aria-pressed')`), 'true', 'a rejected toggle must retain the actual Host state')
  assert.equal(await panel.webContents.executeJavaScript(`document.querySelector('#caption').textContent`), seed.args[0], 'control errors must preserve the caption')
  inputActionError = ''
  inputState.voice = { active: true, source: 'other-owner' }
  inputState.supports_images = false
  publish({ method: 'setAsrStatus', args: [{ status: 'listening', source: 'other-owner' }] })
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#voice').disabled && document.querySelector('#vision').disabled`), 'other owner/model limitations did not disable the buttons')
  const rejectedCount = inputActions.length
  await panel.webContents.executeJavaScript(`document.querySelector('#voice').click(); document.querySelector('#vision').click()`)
  assert.equal(inputActions.length, rejectedCount)
  inputState.voice = { active: false, source: '' }; inputState.supports_images = true
  publish({ method: 'setAsrStatus', args: [{ status: 'idle', source: 'wake' }] })
  await until(() => panel.webContents.executeJavaScript(`!document.querySelector('#voice').disabled && !document.querySelector('#vision').disabled`), 'updated Host inputs did not restore the buttons')
  const longText = '我们先整理已经找到的线索，再决定下一步。'.repeat(12)
  publish({ method: 'setSubtitle', args: [longText] })
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#caption').textContent === ${JSON.stringify(longText)}`), 'long subtitle was truncated')
  await until(() => panel.getBounds().height > 226, 'long caption did not grow the card')
  await until(() => panel.webContents.executeJavaScript('document.querySelector("#caption").scrollHeight <= document.querySelector("#caption").clientHeight'), 'fitting caption should not require scrolling')
  await captureCard(panel, 'companion-long-caption.png')
  publish({ method: 'setSubtitle', args: [longText.repeat(30)] })
  await until(() => panel.getBounds().height === screen.getDisplayMatching(panel.getBounds()).workArea.height, 'caption was not limited to the monitor')
  assert.equal(await panel.webContents.executeJavaScript('document.querySelector("#caption").scrollHeight > document.querySelector("#caption").clientHeight'), true)
  const capped = panel.getBounds()
  assert.deepEqual(clampToArea(capped, screen.getDisplayMatching(capped).workArea), capped)
  publish(seed)
  await until(() => panel.getBounds().height === 226, 'short caption did not shrink the card')
  panel.setSize(320, 226)
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('.portrait').getBoundingClientRect().width === 88`), 'compact layout did not adapt')
  assert.equal(await panel.webContents.executeJavaScript(`document.querySelector('#caption').scrollWidth <= document.querySelector('#caption').clientWidth`), true)
  await captureCard(panel, 'companion-narrow.png')
  panel.setSize(470, 226)
  await until(() => panel.getBounds().height === 226, 'wide caption did not return to minimum height')
  // A user move detaches; subsequent game moves must not pull the card back.
  panel.setPosition(b.x + 15, b.y + 20)
  panel.emit('moved')
  const detached = panel.getBounds()
  game.setPosition(a.x + 20, a.y + 20)
  assert.deepEqual(panel.getBounds(), detached)
  for (const response of clients) response.end()
  await until(() => visibility.at(-1) === false, 'disconnect did not restore original display')
  await until(() => visibility.at(-1) === true, 'reconnect did not recover the companion')
  await panel.webContents.executeJavaScript('document.querySelector("#close").click()')
  await until(() => panel.isDestroyed(), 'close button did not close window')
  assert.equal(visibility.at(-1), false)
  assert.equal(await slice.webContents.executeJavaScript('window.amadeus.getCompanionPanelState()'), false)
  // Reopening and closing through W's adjacent button is the same singleton toggle.
  game.setBounds(original)
  assert.equal(await slice.webContents.executeJavaScript("window.amadeus.toggleCompanionPanel('work-test')"), true)
  const reopened = BrowserWindow.getAllWindows().find(window => window !== game && window !== slice)
  await reopened.webContents.executeJavaScript(`document.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))`)
  await until(() => reopened.isDestroyed(), 'right-click did not close the card')
  assert.equal(await slice.webContents.executeJavaScript("window.amadeus.toggleCompanionPanel('work-test')"), true)
  assert.equal(await slice.webContents.executeJavaScript("window.amadeus.toggleCompanionPanel('work-test')"), false)
  assert.equal(await slice.webContents.executeJavaScript("window.amadeus.toggleCompanionPanel('work-test')"), true)
  const current = BrowserWindow.getAllWindows().find(window => window !== game && window !== slice)
  await until(() => visibility.at(-1) === true, 'reopened card did not attach presentation')
  const currentBounds = current.getBounds()
  reopened.emit('closed') // A delayed event from a retired card has no authority.
  assert.deepEqual(current.getBounds(), currentBounds)
  assert.equal(await slice.webContents.executeJavaScript('window.amadeus.getCompanionPanelState()'), true)
  current.destroy()
  await until(() => visibility.at(-1) === false, 'external close did not restore presentation')
  assert.deepEqual(game.getBounds(), original)
  // Exercise production IPC and native window minimums against a narrow-work-area fixture.
  const matchingDisplay = screen.getDisplayMatching.bind(screen)
  const nearestDisplay = screen.getDisplayNearestPoint.bind(screen)
  const narrowArea = { ...area, width: 1024, height: 728 }
  screen.getDisplayMatching = bounds => ({ ...matchingDisplay(bounds), workArea: narrowArea })
  screen.getDisplayNearestPoint = point => ({ ...nearestDisplay(point), workArea: narrowArea })
  try {
    game.setBounds(narrowArea)
    const originalNarrowGame = game.getBounds()
    await panelHost.toggle('work-test')
    const narrow = BrowserWindow.getAllWindows().find(window => window !== game && window !== slice)
    await until(() => narrow.webContents.executeJavaScript(`document.querySelector('#caption').textContent === ${JSON.stringify(seed.args[0])}`), 'narrow card did not connect')
    const previewHeight = game.getBounds().height
    publish({ method: 'setSubtitle', args: [longText.repeat(30)] })
    await until(() => narrow.webContents.executeJavaScript(`document.querySelector('#caption').textContent.length > 1000`), 'narrow card did not receive the long caption')
    await narrow.webContents.executeJavaScript(`window.companion.fitContent(document.querySelector('#caption').scrollHeight + 120)`)
    const preview = game.getBounds(), card = narrow.getBounds()
    assert.ok(preview.height >= previewHeight - 1, 'caption growth must preserve the stacked preview height')
    assert.deepEqual(clampToArea(card, narrowArea), card, 'stacked card must remain within the work area')
    assert.ok(preview.y + preview.height <= card.y, 'stacked card must not cover the preview')
    assert.equal(await narrow.webContents.executeJavaScript(`document.querySelector('#caption').scrollHeight > document.querySelector('#caption').clientHeight`), true)
    publish(seed)
    await until(() => narrow.webContents.executeJavaScript(`document.querySelector('#caption').textContent === ${JSON.stringify(seed.args[0])}`), 'short caption did not recover')
    await panelHost.close()
    assert.deepEqual(game.getBounds(), originalNarrowGame, 'narrow preview bounds must restore on close')
  } finally {
    screen.getDisplayMatching = matchingDisplay
    screen.getDisplayNearestPoint = nearestDisplay
  }
  console.log(JSON.stringify({ ok: true, checks: ['actual Slice button next to W', 'native dock without overlap', 'optional-art-free portrait fixture', 'VN geometry and opacity', 'saved height fits caption', 'visibility event pauses sweep', 'keyboard reveals contextual controls', 'resize/input IPC rejects foreign/invalid requests', 'Host-backed voice/vision controls', 'pending input click is not duplicated', 'other ASR owner and unsupported images disable inputs', 'rejected input preserves caption and Host state', 'no dock or motion toggle', 'shared subtitles and speaking', 'caption grows and shrinks', 'monitor-limited caption scroll', 'compact layout', 'drag detaches', 'disconnect/reconnect restores presentation', 'close button and right-click', 'singleton toggle', 'retired window events cannot affect the current card', 'external close restores presentation', 'preview bounds restored', 'narrow docking preserves preview while captions scroll'], layout, screenshots: output }))
} catch (error) {
  console.error(error)
  process.exitCode = 1
} finally {
  releaseInput?.()
  await panelHost?.close()
  for (const window of BrowserWindow.getAllWindows()) window.destroy()
  for (const response of clients) response.end()
  server.closeAllConnections()
  server.close()
  app.exit(process.exitCode || 0)
}
}
app.whenReady().then(run)
