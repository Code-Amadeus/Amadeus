// Run with Electron. All Host responses are synthetic; production React/CSS render.
import { app, BrowserWindow } from 'electron'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const electronRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.join(electronRoot, '../output/diagnostics/extension-settings-ui')
// The smoke owns its exit code after asynchronous cleanup.
app.on('window-all-closed', () => {})
app.setPath('userData', path.join(output, 'electron-profile'))
async function run() {
await fs.mkdir(output, { recursive: true })
await app.whenReady()
const { createServer } = await import('vite')
const server = await createServer({ configFile: path.join(electronRoot, 'vite.config.ts'), configLoader: 'runner', root: electronRoot,
  cacheDir: path.join(output, 'vite-cache'), server: { host: '127.0.0.1', port: 0, strictPort: false },
  plugins: [{ name: 'extension-settings-smoke', configureServer(server) {
    server.middlewares.use(async (request, response, next) => {
      if (request.url === '/fixture-theme-background.png') {
        response.setHeader('Content-Type', 'image/png')
        response.end(await fs.readFile(path.join(electronRoot, '../examples/extensions/desktop-theme/src/amadeus_example_theme/background.png')))
        return
      }
      if (request.url?.split('?')[0] !== '/extension-settings-smoke.html') return next()
      response.setHeader('Content-Type', 'text/html; charset=utf-8')
      response.end(await server.transformIndexHtml('/extension-settings-smoke.html', '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/tests/fixtures/extensionSettings.tsx"></script></body></html>'))
    })
  } }],
})
await server.listen()
const url = `http://127.0.0.1:${server.httpServer.address().port}/extension-settings-smoke.html`
const window = new BrowserWindow({ width: 1180, height: 1050, show: false,
  webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false, offscreen: true } })
const ui = source => window.webContents.executeJavaScript(source)
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
async function until(source, message) { const deadline = Date.now() + 20000
  while (Date.now() < deadline) { if (await ui(source)) return; await pause(50) }
  throw new Error(message)
}
async function click(label) { const clicked = await ui(`(() => { const button = [...document.querySelectorAll('button')].find(node => node.textContent.trim() === ${JSON.stringify(label)}); if (!button || button.disabled) return false; button.click(); return true })()`)
  assert.equal(clicked, true, `Enabled button missing: ${label}`); await pause(80)
}
async function configure(cardIndex) { await ui(`[...document.querySelectorAll('.extension-card-heading')[${cardIndex}].closest('.setting-card').querySelectorAll('button')].find(node => node.textContent.trim() === 'Configure extension').click()`); await pause(80) }
async function input(selector, value) { await ui(`(() => { const node = document.querySelector(${JSON.stringify(selector)}); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(node, ${JSON.stringify(value)}); node.dispatchEvent(new Event('input', { bubbles:true })); })()`); await pause(60) }
async function capture(name) { await pause(200); const frame = await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); await fs.writeFile(path.join(output, name), frame.toPNG()) }
const report = { passed: false, checks: [], screenshots: [] }
try {
  await window.loadURL(url)
  await until("document.body.textContent.includes('Example Work tools')", 'Installed packages did not render')
  await capture('packages-desktop-en.png'); report.screenshots.push('packages-desktop-en.png')
  await click('Configure extension')
  assert.equal(await ui("document.querySelector('#extension-config-api_key').value"), '')
  assert.equal(await ui("document.querySelector('#extension-config-api_key').type"), 'password')
  assert.equal(await ui("[...document.querySelectorAll('.settings-form input, .settings-form select')].every(node => node.labels?.length > 0)"), true)
  await input('#extension-config-voice', 'unsubmitted voice')
  await input('#extension-config-api_key', 'synthetic replacement')
  await ui('window.extensionFixture.setFailSave(true)')
  await click('Save extension')
  await until("document.body.textContent.includes('Synthetic save failure')", 'Save failure was not visible')
  await ui('window.extensionFixture.refreshRuntime()')
  assert.equal(await ui("document.querySelector('#extension-config-voice').value"), 'unsubmitted voice')
  assert.equal(await ui("document.querySelector('#extension-config-api_key').value"), 'synthetic replacement')
  report.checks.push('Credential input starts empty and masked; labels exist; failed save and runtime refresh preserve draft')
  await ui('window.extensionFixture.setFailSave(false)')
  await click('Save extension')
  await until("!document.querySelector('form[aria-label=\"Configure extension\"]')", 'Successful save did not close editor')
  assert.equal(await ui("window.extensionFixture.snapshot().restartRequired"), true)
  await click('Open voice settings')
  await until("document.querySelector('select[aria-label=\"Speech synthesis backend\"]') !== null || [...document.querySelectorAll('select')].some(node => [...node.options].some(option => option.value==='org.example.voice/speech'))", 'Shared speech selector did not include installed voice')
  assert.equal(await ui("[...document.querySelectorAll('select')].filter(node => [...node.options].some(option => option.value==='org.example.voice/speech')).length"), 1)
  report.checks.push('Single production speech selector includes installed extension voice')
  await click('Manage community voices')
  await configure(1)
  await until("document.querySelector('#extension-config-endpoint') !== null", 'MCP package editor did not open')
  assert.equal(await ui("document.querySelector('.settings-form-provider-choices').textContent.includes('Codex')"), true)
  assert.equal(await ui("document.querySelector('.settings-form-provider-choices').textContent.includes('Pi')"), false)
  assert.equal(await ui("document.querySelector('.settings-form-provider-choices').textContent.includes('Incompatible agent')"), false)
  await ui("document.querySelector('.settings-form-provider-choices input').click()")
  await pause(80); await click('Save extension')
  assert.deepEqual(await ui("window.extensionFixture.snapshot().extensions.find(item=>item.id==='org.example.tools').providers"), ['codex'])
  report.checks.push('MCP binding lists only compatible Work Provider and saves selection pending restart')
  await configure(2)
  await until("document.querySelector('.settings-form-provider-choices') !== null", 'Skill editor did not open')
  assert.equal(await ui("document.querySelector('.settings-form-provider-choices').textContent.includes('Codex ACP')"), true)
  assert.equal(await ui("document.querySelector('.settings-form-provider-choices').textContent.includes('Pi')"), true)
  assert.equal(await ui("document.querySelector('.settings-form-provider-choices input').checked"), false)
  await ui("document.querySelectorAll('.settings-form-provider-choices input').forEach(node=>node.click())")
  await pause(80)
  await capture('skill-agent-bindings.png'); report.screenshots.push('skill-agent-bindings.png')
  await click('Save extension')
  assert.deepEqual(await ui("window.extensionFixture.snapshot().extensions.find(item=>item.id==='org.example.skill').providers"), ['codex', 'codex_acp', 'pi'])
  report.checks.push('One Skill installation binds Codex, ACP and Pi; incompatible Agents are excluded and save requires restart')
  await ui("document.querySelector('[id=\"extension-enabled-org.example.skill\"]').click()")
  await until("window.extensionFixture.snapshot().extensions.find(item=>item.id==='org.example.skill').enabled", 'Card switch did not enable the Skill package')
  report.checks.push('The package card switch enables a package without reopening its editor')
  await click('Restart backend to apply')
  await until("window.extensionFixture.snapshot().restartRequired === false", 'Synthetic restart did not apply snapshot')
  await click('Open voice settings')
  await until("[...document.querySelectorAll('select')].some(node => [...node.options].some(option=>option.value==='org.example.voice/speech'))", 'Shared voice selector did not reappear')
  await ui("(() => { const selector=[...document.querySelectorAll('select')].find(node=>[...node.options].some(option=>option.value==='org.example.voice/speech')); selector.value='org.example.voice/speech'; selector.dispatchEvent(new Event('change',{bubbles:true})); })()")
  await until("window.extensionFixture.snapshot().values.TTS_BACKEND === 'org.example.voice/speech'", 'Extension voice selection did not save')
  assert.equal(await ui("window.extensionFixture.calls.some(call => call.method==='system.set_config')"), false)
  await click('Manage community voices')
  await click('Remove')
  await until("document.body.textContent.includes('This package contains the saved speech engine.')", 'Selected voice removal warning missing')
  assert.equal(await ui("document.querySelector('.extension-removal-data input').checked"), false)
  await capture('removal-desktop-en.png'); report.screenshots.push('removal-desktop-en.png')
  await click('Confirm remove')
  await until("window.extensionFixture.snapshot().extensions[0].pendingRemoval", 'Uninstall did not save pending state')
  assert.equal(await ui("document.body.textContent.includes('Removal pending restart')"), true)
  await click('Restart backend to apply')
  await until("!window.extensionFixture.snapshot().extensions.some(item=>item.id==='org.example.voice')", 'Synthetic restart did not remove package')
  await ui("window.extensionFixture.openSettings('voice')")
  await until("[...document.querySelectorAll('select')].some(node=>[...node.options].some(option=>option.value==='org.example.voice/speech' && option.disabled && option.selected))", 'Missing selected voice was not retained as unavailable')
  assert.equal(await ui("document.body.textContent.includes('speech is unavailable while text responses continue')"), true)
  report.checks.push('Selected voice removal warns, retains data by default and preserves unavailable TTS identity after restart without a hot-switch RPC')
  await window.setSize(560, 950)
  await window.loadURL(url + '?locale=zh-CN&theme=wallpaper-slice')
  await until("document.body.textContent.includes('示例英语语音')", 'Chinese extension page did not render')
  await capture('packages-narrow-zh.png'); report.screenshots.push('packages-narrow-zh.png')
  await click('配置扩展')
  await until("document.querySelector('#extension-config-api_key') !== null", 'Chinese editor did not open')
  await capture('editor-narrow-zh.png'); report.screenshots.push('editor-narrow-zh.png')
  assert.equal(await ui('document.documentElement.scrollWidth <= innerWidth'), true)
  assert.equal(await ui("[...document.querySelectorAll('.settings-form input, .settings-form select')].every(node => { const box=node.getBoundingClientRect(); return box.left>=0 && box.right<=innerWidth && node.labels?.length>0 })"), true)
  assert.deepEqual(await ui('window.extensionFixture.errors'), [])
  report.checks.push('Chinese dark-theme editor fits 560px window; fields have labels; no browser errors')
  for (const [locale, palette, base, width] of [['en-US', 'day', 'classic', 1100], ['zh-CN', 'night', 'wallpaper-slice', 620]]) {
    window.setSize(width, 950)
    for (const presentation of ['plugins', 'settings']) {
      await window.loadURL(`${url}?locale=${locale}&palette=${palette}&theme=${base}&presentation=${presentation}&section=general`)
      await until(`document.documentElement.dataset.themeExtension?.endsWith('/${palette}')`, 'Production theme did not render')
      assert.equal(await ui('document.documentElement.dataset.theme'), base)
      assert.equal(await ui('document.querySelectorAll("style[data-amadeus-extension-theme]").length'), 1)
      assert.equal(await ui('document.documentElement.scrollWidth <= innerWidth'), true)
      assert.equal(await ui('getComputedStyle(document.body).backgroundImage'), 'none', 'Base gradient must not cover the extension canvas')
      assert.equal(await ui('getComputedStyle(document.getElementById("root")).backgroundImage'), 'none')
      if (palette === 'night') assert.match(await ui('getComputedStyle(document.documentElement).backgroundImage'), /blob:/)
      if (presentation === 'plugins') assert.equal(await ui(`document.body.textContent.includes(${JSON.stringify(locale === 'en-US' ? 'Quiet desktop' : '静谧桌面')})`), true)
      const name = `theme-${palette}-${presentation}-${locale}-${width}.png`
      await capture(name); report.screenshots.push(name)
      assert.deepEqual(await ui('window.extensionFixture.errors'), [])
    }
  }
  report.checks.push('Production themes preserve both base identities, show native manager and Appearance at 1100px English / 620px Chinese, and leave one style with no covering base gradients')
  await window.loadURL(url + '?presentation=plugins&section=graphics')
  await until("document.body.textContent.includes('Example English voice')", 'Plugin navigation fixture not ready')
  assert.equal(await ui("localStorage.getItem('amadeus.settings.section')"), 'graphics')
  await click('Open voice settings')
  await until("document.querySelector('.settings-page-title')?.textContent === 'Settings'", 'Voice link did not open Settings')
  assert.equal(await ui("localStorage.getItem('amadeus.settings.section')"), 'voice')
  await click('General')
  await click('Manage plugins')
  await until("document.body.textContent.includes('Community extensions')", 'Could not return to Plugins')
  assert.equal(await ui("localStorage.getItem('amadeus.settings.section')"), 'general')
  report.checks.push('Plugin voice link opens Voice settings; General Manage plugins returns to the manager and preserves General')
  report.passed = true
  console.log(JSON.stringify(report, null, 2))
} catch (error) {
  report.error = String(error?.stack || error)
  report.rendererErrors = await ui('window.extensionFixture?.errors || []').catch(() => [])
  report.theme = await ui('({ ...document.documentElement.dataset })').catch(() => ({}))
  report.themeResponse = await ui('window.amadeus.getDesktopTheme().then(value => ({ base: value.base, reason: value.reason }), error => ({ error: String(error) }))').catch(error => String(error))
  await capture('failure.png').catch(() => {})
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  window.destroy(); await server.close(); app.exit(report.passed ? 0 : 1)
}
}
run().catch(reason => { console.error(reason); app.exit(1) })
