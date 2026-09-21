// Native Chromium interaction contract. The bridge is a local fixture: no
// microphone, model, user Session, desktop capture or settings are touched.
const { app, BrowserWindow } = require('electron')
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const root = path.resolve(__dirname, '../..')
const output = path.join(root, 'electron/build')
fs.mkdirSync(output, { recursive: true })
app.setPath('userData', path.join(output, 'composer-smoke-profile'))
const actions = [], streams = []
let voice = { active: false, source: '', continuous: false }, watching = false, failNext = false
const server = http.createServer(async (req, res) => {
  const route = new URL(req.url, 'http://localhost').pathname
  const json = value => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)) }
  if (route === '/wallpaper/bridge-info') return json({
    bridgePort: server.address().port, bridgeToken: 'fixture-token',
    sliceBounds: { x: 0, y: 0, width: 1, height: 1 },
    canvasBounds: { x: .05, y: .05, width: .9, height: .4 },
    keyboardInputToggleBounds: { x: .64, y: .86, width: .28, height: .05 },
    keyboardComposerBounds: { x: .13, y: .67, width: .74, height: .18 },
  })
  if (route === '/wallpaper/canvas-events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    streams.push(res)
    res.write('data: ' + JSON.stringify({ method: 'setCanvas', args: [{ title: 'Existing work', text: 'Restored work', expanded: true }] }) + '\n\n')
    return
  }
  if (route === '/wallpaper/chat-action') {
    assert.equal(req.headers['x-amadeus-bridge-token'], 'fixture-token')
    let body = ''; for await (const chunk of req) body += chunk
    const payload = JSON.parse(body); actions.push(payload)
    if (payload.action === 'status') return json({ ok: true, supports_images: true, voice, wake: { running: !voice.active }, watching })
    if (failNext) { failNext = false; res.statusCode = 400; return json({ ok: false, error: 'injected_failure' }) }
    if (payload.action === 'voice_start') voice = { active: true, source: 'wake', continuous: true }
    if (payload.action === 'voice_stop' || payload.action === 'new_chat') voice = { active: false, source: '', continuous: false }
    if (payload.action === 'vision_toggle') watching = !watching
    if (payload.action === 'vision_windows') return json({ ok: true, windows: [{ hwnd: '42', title: 'Sample editor' }] })
    return json({ ok: true })
  }
  if (!route.startsWith('/render/web/') || route.includes('..')) { res.statusCode = 404; return res.end() }
  const file = path.join(root, route)
  if (!fs.existsSync(file)) { res.statusCode = 404; return res.end() }
  res.setHeader('Content-Type', route.endsWith('.html') ? 'text/html; charset=utf-8' : 'text/javascript; charset=utf-8')
  res.end(fs.readFileSync(file))
})
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let win
const evaluate = source => win.webContents.executeJavaScript(source)
async function until(source) {
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) { if (await evaluate(source)) return; await pause(30) }
  throw new Error('Timed out: ' + source)
}
async function press(selector, long = false, button = 'left') {
  const point = await evaluate(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return { x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2) } })()`)
  win.webContents.sendInputEvent({ type: 'mouseMove', ...point })
  win.webContents.sendInputEvent({ type: 'mouseDown', ...point, button, clickCount: 1 })
  if (long) await pause(650)
  win.webContents.sendInputEvent({ type: 'mouseUp', ...point, button, clickCount: 1 })
  await pause(80)
}
async function screenshot(name) { win.showInactive(); await pause(400); fs.writeFileSync(path.join(output, name), (await win.webContents.capturePage()).toPNG()) }
app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  win = new BrowserWindow({ width: 780, height: 640, show: true, webPreferences: { sandbox: true, backgroundThrottling: false } })
  win.webContents.on('console-message', event => { if (event.level === 'error') console.error(event.message) })
  const url = `http://127.0.0.1:${server.address().port}/render/web/electron_slice.html`
  await win.loadURL(url + '?windowsComposer=1')
  await until(`document.querySelector('.composer-voice small')?.textContent === 'Zz'`)
  assert.equal(await evaluate(`document.querySelector('#wallpaper-keyboard-composer').hidden`), true)
  assert.equal(await evaluate(`document.activeElement.tagName === 'TEXTAREA'`), false)
  assert.equal(await evaluate(`Boolean(document.querySelector('.crt-canvas-surface.expanded'))`), false)
  console.log('PASS startup collapsed')
  await screenshot('composer-collapsed.png')
  await press('.wallpaper-keyboard-composer-toggle')
  await until(`!document.querySelector('#wallpaper-keyboard-composer').hidden`)
  await until(`!document.querySelector('.composer-image').disabled`)
  await evaluate(`window.filePickerCalls = 0; document.querySelector('.composer-file').click = () => window.filePickerCalls++; void 0`)
  await press('.composer-image')
  assert.equal(await evaluate('window.filePickerCalls'), 1)
  await press('.composer-image', true)
  await until(`document.querySelector('.composer-image').getAttribute('aria-pressed') === 'true'`)
  assert.equal(await evaluate('window.filePickerCalls'), 1, 'long press must not also upload')
  await press('.composer-image', false, 'right')
  await until(`document.querySelector('.composer-window-picker').textContent.includes('Sample editor')`)
  await screenshot('composer-window-picker.png')
  await evaluate(`Array.from(document.querySelectorAll('.composer-window-picker button')).find(b => b.textContent === 'Sample editor').click()`)
  await until(`document.querySelector('.composer-window-picker').hidden`)
  assert(actions.some(a => a.action === 'vision_select' && a.hwnd === '42'))
  await press('.composer-voice')
  await until(`document.querySelector('.composer-voice').dataset.voiceState === 'continuous'`)
  await press('.composer-voice', true)
  await until(`document.querySelector('.composer-voice').dataset.voiceState === 'sleeping'`)
  await press('.composer-voice', true)
  await until(`document.querySelector('.composer-voice').dataset.voiceState === 'continuous'`)
  assert.equal(actions.filter(a => a.action === 'voice_start').length, 2)
  assert.equal(actions.filter(a => a.action === 'voice_stop').length, 1)
  await press('.wallpaper-keyboard-composer-close')
  assert.equal(await evaluate(`document.querySelector('.wallpaper-keyboard-composer-toggle-indicator').dataset.voiceState`), 'continuous')
  await press('.wallpaper-keyboard-composer-toggle-indicator')
  await press('.composer-voice')
  await until(`document.querySelector('.composer-voice small').textContent === 'Zz'`)
  await evaluate(`(() => {
    const canvas = document.createElement('canvas'); canvas.width = 32; canvas.height = 24;
    canvas.getContext('2d').fillRect(0, 0, 32, 24);
    canvas.toBlob(blob => { const files = new DataTransfer(); files.items.add(new File([blob], 'sample.png', {type:'image/png'}));
      const input = document.querySelector('.composer-file'); input.files = files.files; input.dispatchEvent(new Event('change')); });
  })()`)
  await until(`!document.querySelector('.composer-attachment').hidden && !document.querySelector('.wallpaper-keyboard-composer-send').disabled`)
  await screenshot('composer-expanded.png')
  const crop = await evaluate(`(() => { const r = document.querySelector('#wallpaper-keyboard-composer').getBoundingClientRect(); return {x: Math.floor(r.x)-10, y: Math.floor(r.y)-10, width: Math.ceil(r.width)+20, height: Math.ceil(r.height)+20} })()`)
  fs.writeFileSync(path.join(output, 'composer-detail.png'), (await win.webContents.capturePage(crop)).toPNG())
  failNext = true
  await press('.wallpaper-keyboard-composer-send')
  await until(`document.querySelector('.wallpaper-keyboard-composer-status').textContent === '请重试'`)
  assert.equal(await evaluate(`document.querySelector('.composer-attachment').hidden`), false)
  await press('.wallpaper-keyboard-composer-send')
  await until(`document.querySelector('.composer-attachment').hidden`)
  const sent = actions.find(a => a.visual)
  assert.equal(sent.visual.frame.mime, 'image/jpeg')
  assert.equal(sent.visual.frame.width, 32)
  assert(sent.visual.frame.dataUrl.startsWith('data:image/jpeg;base64,'))
  await evaluate(`document.querySelector('textarea').value = 'keep on failure'`)
  failNext = true
  await press('.composer-new-chat')
  assert.equal(await evaluate(`document.querySelector('textarea').value`), 'keep on failure')
  await press('.composer-new-chat')
  await until(`document.querySelector('textarea').value === ''`)
  // A page without the Windows opt-in keeps the existing platform behavior.
  await win.loadURL(url)
  await until(`!document.querySelector('#wallpaper-keyboard-composer').hidden`)
  assert.equal(await evaluate(`document.querySelector('.composer-voice')`), null)
  await until(`Boolean(document.querySelector('.crt-canvas-surface.expanded'))`)
  console.log('PASS collapsed startup, legacy platform, image upload/retry, long press, window selection, voice modes and new chat')
  app.exit(0)
}).catch(async error => { console.error(error); if (win) await screenshot('composer-failure.png').catch(() => {}); app.exit(1) })
setTimeout(() => { console.error('Composer smoke timed out'); app.exit(1) }, 45000).unref()
