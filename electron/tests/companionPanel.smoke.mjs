// Run with Electron after `npm run build`. All windows stay hidden; no backend or audio is started.
import { app, BrowserWindow, screen } from 'electron'
import assert from 'node:assert/strict'
import http from 'node:http'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { CompanionPanel } from '../dist/main/companionPanel.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const output = path.join(root, 'output/diagnostics/companion-panel')
await fs.mkdir(output, { recursive: true })
app.setPath('userData', path.join(output, 'electron-profile'))
app.disableHardwareAcceleration()
BrowserWindow.prototype.showInactive = function () { this.webContents.setBackgroundThrottling(false) } // Hidden diagnostic rendering only.
const clients = new Set()
const visibility = []
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
async function run() {
try {
  const area = screen.getPrimaryDisplay().workArea
  const game = new BrowserWindow({ ...area, minWidth: 720, minHeight: 520, show: false })
  const original = game.getBounds()
  const slice = new BrowserWindow({ show: false, webPreferences: { preload: path.join(root, 'electron/dist/preload/slice.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false } })
  slice.webContents.on('console-message', event => { console.error('[slice test]', event.message) })
  panelHost = new CompanionPanel({ userDataDir: output,
    preload: path.join(root, 'electron/dist/preload/companion.cjs'),
    portraitCacheDir: path.resolve(root, '../visual novel player/out/vn_portrait_cache'),
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
  await until(() => visibility.at(-1) === true, 'original display was not suppressed')
  await until(() => panel.webContents.executeJavaScript('document.querySelector("#portrait").naturalWidth > 0'), 'VN portrait did not load')
  const a = game.getBounds(), b = panel.getBounds()
  assert.equal(a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y, false)
  publish({ method: 'triggerSpriteForgeIntent', args: ['trans_smile'] })
  publish({ method: 'setSpeaking', args: [true] })
  await until(() => panel.webContents.executeJavaScript('document.body.classList.contains("speaking")'), 'speaking did not reach card')
  await new Promise(resolve => setTimeout(resolve, 400))
  await fs.writeFile(path.join(output, 'companion-card.png'), (await panel.webContents.capturePage()).toPNG())
  const longText = '我们先整理已经找到的线索，再决定下一步。'.repeat(18)
  publish({ method: 'setSubtitle', args: [longText] })
  await until(() => panel.webContents.executeJavaScript(`document.querySelector('#caption').textContent === ${JSON.stringify(longText)}`), 'long subtitle was truncated')
  assert.equal(await panel.webContents.executeJavaScript('document.querySelector("#caption").scrollHeight > document.querySelector("#caption").clientHeight'), true)
  await fs.writeFile(path.join(output, 'companion-long-caption.png'), (await panel.webContents.capturePage()).toPNG())
  // A user move detaches; subsequent game moves must not pull the card back.
  panel.setPosition(b.x + 15, b.y + 20)
  panel.emit('moved')
  const detached = panel.getBounds()
  game.setPosition(a.x + 20, a.y + 20)
  assert.deepEqual(panel.getBounds(), detached)
  assert.equal(await panel.webContents.executeJavaScript('window.companion.dock()'), true)
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
  assert.equal(await slice.webContents.executeJavaScript("window.amadeus.toggleCompanionPanel('work-test')"), false)
  assert.deepEqual(game.getBounds(), original)
  console.log(JSON.stringify({ ok: true, checks: ['actual Slice button next to W', 'native dock without overlap', 'real VN frames', 'shared subtitles and speaking', 'long caption scroll', 'drag detaches', 'redock', 'disconnect/reconnect restores presentation', 'close button', 'singleton toggle', 'preview bounds restored'], screenshots: output }))
} catch (error) {
  console.error(error)
  process.exitCode = 1
} finally {
  await panelHost?.close()
  for (const window of BrowserWindow.getAllWindows()) window.destroy()
  for (const response of clients) response.end()
  server.closeAllConnections()
  server.close()
  app.exit(process.exitCode || 0)
}
}
app.whenReady().then(run)
