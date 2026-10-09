// Run with Electron and explicit local model/Core paths. Host RPCs are test
// fixtures; preview pixel output comes from the production Cubism runtime.
import { app, BrowserWindow } from 'electron'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const electronRoot = path.join(root, 'electron')
function option(name) {
  const index = process.argv.indexOf(name)
  return index < 0 ? '' : process.argv[index + 1] || ''
}
const modelPath = path.resolve(option('--model'))
const corePath = path.resolve(option('--core'))
const output = path.resolve(option('--output') || path.join(root, 'output/diagnostics/character-visuals'))
if (!option('--model') || !option('--core')) throw new Error('Supply --model and --core with local files.')
if (!output.startsWith(root + path.sep)) throw new Error('Smoke output must stay in this experimental worktree.')
app.setPath('userData', path.join(output, 'electron-profile'))
app.commandLine.appendSwitch('use-angle', 'swiftshader')
app.commandLine.appendSwitch('enable-unsafe-swiftshader')

async function run() {
const { createServer } = await import('vite')
await fs.mkdir(output, { recursive: true })
await fs.access(corePath)
const settings = JSON.parse(await fs.readFile(modelPath, 'utf8'))
const expressions = settings.FileReferences.Expressions.map(item => item.Name)
const lipIds = (settings.Groups || []).filter(group => group.Target === 'Parameter' && group.Name === 'LipSync').flatMap(group => group.Ids)
const mapping = { normal: null, neutral: null, smile: 'Smile', happy: 'Smile', thinking: 'Thinking',
  angry: 'Angry', sad: 'Disappointed', disappointed: 'Disappointed', work: 'Thinking', working: 'Thinking',
  serious_speaking: 'Thinking', shy: null, blush: null, surprised: null }
for (const key of Object.keys(mapping)) if (!expressions.includes(mapping[key])) mapping[key] = null
const seed = {
  locale: 'zh-CN', core_path: corePath,
  model_url: pathToFileURL(modelPath).href, core_url: pathToFileURL(corePath).href,
  preview_url: pathToFileURL(path.join(root, 'render/web/visual_preview.html')).href,
  capabilities: { expressions, lip_sync_ids: lipIds, warnings: [] },
  profile: {
    profile_id: 'inspection-fixture', kind: 'live2d', name: path.basename(modelPath, '.model3.json'),
    model_path: modelPath, emotion_map: mapping,
    mouth: { gain: 1, smoothing_ms: 60, parameter_ids: [] },
    layouts: { render: { scale: 1, x: 0, y: 0 }, wallpaper: { scale: 1, x: 0, y: 0 } },
  },
}
await app.whenReady()
const server = await createServer({
  configFile: path.join(electronRoot, 'vite.config.ts'), configLoader: 'runner', root: electronRoot,
  cacheDir: path.join(root, 'runtime/character-visuals-vite-cache'),
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  plugins: [{
    name: 'character-visuals-smoke',
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        if (request.url?.split('?')[0] !== '/character-visuals-smoke.html') return next()
        response.setHeader('Content-Type', 'text/html; charset=utf-8')
        const html = '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div>'
          + '<script>window.visualSeed=' + JSON.stringify(seed).replaceAll('<', '\\u003c') + '</script>'
          + '<script type="module" src="/tests/fixtures/characterVisuals.tsx"></script></body></html>'
        response.end(await server.transformIndexHtml('/character-visuals-smoke.html', html))
      })
    },
  }],
})
await server.listen()
const port = server.httpServer.address().port
const window = new BrowserWindow({ width: 1440, height: 1200, show: false, autoHideMenuBar: true,
  webPreferences: { webSecurity: false, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false, offscreen: true } })
window.webContents.setFrameRate(30)
const ui = script => window.webContents.executeJavaScript(script)
async function until(script, message, timeout = 25000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await ui(script)) return
    await new Promise(resolve => setTimeout(resolve, 60))
  }
  throw new Error(message)
}
async function click(label) {
  const result = await ui('(() => { const b = [...document.querySelectorAll("button")].find(b => b.textContent.trim() === '
    + JSON.stringify(label) + '); if (!b || b.disabled) return false; b.click(); return true })()')
  assert.equal(result, true, 'Enabled button missing: ' + label)
}
async function select(selector, value) {
  await ui('(() => { const e=document.querySelector(' + JSON.stringify(selector) + '); e.value='
    + JSON.stringify(value) + '; e.dispatchEvent(new Event("change",{bubbles:true})) })()')
}
async function input(selector, value) {
  await ui('(() => { const e=document.querySelector(' + JSON.stringify(selector) + ');'
    + 'Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(e,'
    + JSON.stringify(value) + '); e.dispatchEvent(new Event("input",{bubbles:true})); e.dispatchEvent(new Event("change",{bubbles:true})) })()')
}
async function capture(name) {
  await ui('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
  // Hidden compositor rasterization can lag the DOM paint boundary.
  await new Promise(resolve => setTimeout(resolve, 250))
  const frame = await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })
  const png = frame.toPNG()
  assert.ok(png.length > 33 && png.subarray(0, 8).toString('hex') === '89504e470d0a1a0a', 'Screenshot must contain a PNG frame')
  const width = png.readUInt32BE(16), height = png.readUInt32BE(20)
  assert.ok(width > 0 && height > 0, 'Screenshot dimensions must be nonzero')
  await fs.writeFile(path.join(output, name), png)
  report.screenshot_dimensions[name] = { width, height, bytes: png.length }
}
const report = { host: 'control fixture', runtime: 'production visual_preview + Live2DCharacter', checks: [], screenshots: [], screenshot_dimensions: {}, passed: false }
await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
try {
  await window.loadURL('http://127.0.0.1:' + port + '/character-visuals-smoke.html')
  await until('[...document.querySelectorAll("button")].some(button=>button.textContent.trim()==="设置")', 'Main Settings entry did not load')
  assert.equal(await ui('[...document.querySelectorAll("nav button")].some(button=>button.textContent.trim()==="角色形象")'), false)
  await click('设置')
  await until('document.querySelector(".settings-section-nav") !== null', 'Production Settings page did not open')
  assert.equal(await ui('document.querySelector("#visual-backend") === null'), true)
  assert.equal(await ui('getComputedStyle([...document.querySelectorAll("nav button")].find(button=>button.textContent.trim()==="设置")).borderLeftWidth'), '3px')
  assert.equal(await ui('window.visualFixture.calls.some(call=>call.method.startsWith("visual."))'), false)
  assert.equal(await ui('[...document.querySelectorAll("nav")].find(nav=>!nav.classList.contains("settings-section-nav")).textContent.includes("角色形象")'), false)
  await capture('settings-entry.png')
  report.screenshots.push('settings-entry.png')
  await click('角色')
  await until('[...document.querySelectorAll("button")].some(button=>button.textContent.trim()==="重启后端" && !button.disabled)', 'Role restart must remain available without a desktop revision')
  await click('外观')
  await until('document.querySelector("#visual-backend")?.value === "sprite"', 'Character Appearance tab did not load')
  assert.equal(await ui('document.querySelector(".character-workspace-tabs [aria-selected=true]").textContent.trim()'), '外观')
  report.checks.push('Settings contains Characters; its Appearance tab lazily opens the visual editor')
  assert.equal(await ui('document.querySelector("iframe") === null'), true)
  report.checks.push('default Sprite; no preview or Cubism instance')
  await capture('sprite-default.png')
  report.screenshots.push('sprite-default.png')

  await click('添加 Live2D 模型…')
  await until('document.querySelector("#visual-profile-name")?.value.length > 0', 'Model profile was not added')
  await click('选择 Core…')
  await until('document.querySelector("#visual-core-path")?.value.length > 0', 'Local Core path was not selected')
  await select('#visual-backend', 'live2d')
  assert.equal(await ui('window.visualFixture.snapshot().config.backend'), 'sprite')
  report.checks.push('draft selection is distinct from applied Sprite')

  await click('打开预览')
  await until('document.querySelector(".visuals-actions .visuals-state.ready") !== null', 'Actual draft model did not become ready')
  await click('Smile')
  await until('document.querySelector(".visuals-column:nth-child(2)").textContent.includes("当前表情: Smile")', 'Expression diagnostic did not confirm Smile')
  await input('#visual-mouth-test', '0.75')
  await capture('live2d-draft-preview.png')
  report.screenshots.push('live2d-draft-preview.png')
  assert.equal(await ui('window.visualFixture.calls.some(call => ["render.mouth","render.speaking","visual.status"].includes(call.method))'), false)
  report.checks.push('real model ready with structured diagnostic; expression and mouth tests isolated')

  await input('#visual-profile-name', '草稿形象')
  const nameBefore = await ui('document.querySelector("#visual-profile-name").value')
  assert.equal(nameBefore, '草稿形象')
  await ui('window.visualFixture.emit("visual.updated",{...window.visualFixture.snapshot(),surfaces:{wallpaper:{runtime_id:"visual-gui-smoke",state:"ready",profile_id:"inspection-fixture",revision:0,diagnostic:{expression:"Smile",mouth_ids:["PARAM_MOUTH_OPEN_Y"],warnings:["Test warning"],render_texture:null}}}})')
  assert.equal(await ui('document.querySelector("#visual-profile-name").value'), nameBefore)
  assert.equal(await ui('document.body.textContent.includes("Test warning")'), true)
  report.checks.push('Host status update preserves draft and renders object diagnostics')
  await click('重新加载模型')
  await until('window.visualFixture.calls.some(call => call.method === "visual.reload")', 'Applied reload did not run')
  assert.equal(await ui('document.querySelector("#visual-profile-name").value'), nameBefore)
  assert.equal(await ui('window.visualFixture.snapshot().config.backend'), 'sprite')
  report.checks.push('reload uses applied configuration and preserves draft')

  await click('刷新预览')
  await until('document.querySelector(".visuals-actions .visuals-state.ready") !== null', 'Refreshed draft did not load')
  await click('复位预览')
  assert.equal(await ui('document.querySelector("#visual-mouth-test").value'), '0')
  await click('保存并应用')
  await until('window.visualFixture.snapshot().config.backend === "live2d"', 'Draft was not saved')
  assert.equal(await ui('window.visualFixture.snapshot().config.profiles[0].name'), nameBefore)
  await click('关闭预览')
  await until('document.querySelector("iframe") === null', 'Preview frame remained after closing')
  report.checks.push('reset closes mouth; save applies profile; closing removes the independent instance')
  await ui('document.querySelector(".settings-scroll-area").scrollTop=0')
  await capture('live2d-saved.png')
  report.screenshots.push('live2d-saved.png')


  await click('打开预览')
  await until('document.querySelector(".visuals-actions .visuals-state.ready") !== null', 'Saved model preview did not reload')
  await click('身份与人格')
  await until('document.querySelector("#visual-backend") === null && document.querySelector("iframe") === null', 'Leaving the visual section did not dispose its preview')
  assert.equal(await ui('document.querySelector(".character-workspace-tabs [aria-selected=true]").textContent.trim()'), '身份与人格')
  await click('外观')
  await until('document.querySelector("#visual-profile-name")?.value === "草稿形象"', 'Returning to the visual section did not restore saved configuration')
  assert.equal(await ui('document.querySelector("iframe") === null'), true)
  report.checks.push('leaving the Appearance tab disposes its preview; returning restores saved profiles without a hidden instance')

  await input('#visual-model-path', path.join(path.dirname(modelPath), 'missing.model3.json'))
  await click('检查并更新')
  await until('document.querySelector("[role=alert]")?.textContent.includes("does not exist")', 'Bad model error was not shown')
  assert.equal(await ui('window.visualFixture.snapshot().config.profiles[0].model_path'), modelPath)
  report.checks.push('bad model displays error and cannot mutate applied configuration')

  const errors = await ui('window.visualFixture.errors')
  assert.deepEqual(errors, [])
  report.errors = errors
  report.passed = true
  console.log(JSON.stringify(report))
} catch (error) {
  report.passed = false
  report.failure = String(error.stack || error)
  try { report.errors = await ui('window.visualFixture?.errors || []'); await capture('failure.png') } catch {}
  console.error(JSON.stringify(report))
  process.exitCode = 1
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  window.destroy()
  await server.close()
  app.exit(process.exitCode || 0)
}
}
app.whenReady().then(run).catch(error => { console.error(error.stack || error); app.exit(1) })
