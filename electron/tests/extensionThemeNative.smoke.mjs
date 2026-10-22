// Native decoder and actual Chromium paint checks. No mocked Electron image API.
import { app, BrowserWindow } from 'electron'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const output = path.join(root, 'output/diagnostics/extension-theme-native')
fs.mkdirSync(output, { recursive: true })
app.setPath('userData', path.join(output, 'profile'))
const report = { passed: false, checks: [] }
async function run() {
let window
try {
  await app.whenReady()
  const { ExtensionThemeResources } = await import('../dist/main/extensionThemes.js')
  const { validateThemePalette, parseThemeColor } = await import('../dist/shared/extensionTheme.js')
  const { loadTypeScript } = await import('./helpers/loadTypeScript.mjs')
  const { createThemeApplication } = loadTypeScript(new URL('../src/renderer/themeApplication.ts', import.meta.url))
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/extension_protocol_theme.json'), 'utf8'))
  const resources = new ExtensionThemeResources()
  const png = fs.readFileSync(path.join(root, 'examples/extensions/desktop-theme/src/amadeus_example_theme/background.png'))
  const { nativeImage } = await import('electron')
  const { readExtensionImage } = await import('../dist/main/extensionImages.js')
  fs.writeFileSync(path.join(output, 'sample.png'), png)
  fs.writeFileSync(path.join(output, 'sample.jpg'), nativeImage.createFromBuffer(png).toJPEG(90))
  for (const name of ['sample.png', 'sample.jpg']) {
    const definition = structuredClone(manifest)
    definition.contributes.theme[0].background = { image: name, fit: 'cover', dim: .8 }
    const applied = await resources.validate(definition, output, 'night')
    assert.ok(applied.night.image.base64)
  }
  for (const name of ['bad.webp', 'disguised.png']) {
    fs.writeFileSync(path.join(output, name), Buffer.from('RIFF0000WEBPnot-a-png'))
    const definition = structuredClone(manifest)
    definition.contributes.theme[0].background = { image: name, fit: 'cover', dim: .8 }
    await assert.rejects(resources.validate(definition, output), /background.image|background/)
  }
  report.checks.push('Real PNG/JPEG decoding succeeds; WebP declarations and disguised bytes are rejected')
  const iconLimits = { label: 'Package icon', maxMiB: 1, maxDimension: 1024 }
  for (const name of ['sample.png', 'sample.jpg']) assert.ok(readExtensionImage(output, name, iconLimits).base64)
  const hugeIcon = Buffer.from(png); hugeIcon.writeUInt32BE(1025, 16)
  fs.writeFileSync(path.join(output, 'huge-icon.png'), hugeIcon)
  assert.throws(() => readExtensionImage(output, 'huge-icon.png', iconLimits), /dimensions/)
  fs.writeFileSync(path.join(output, 'broken-icon.png'), png.subarray(0, 24))
  assert.throws(() => readExtensionImage(output, 'broken-icon.png', iconLimits), /decoded dimensions/)
  report.checks.push('Package icons use the native PNG/JPEG decoder with smaller canvas bounds and reject undecodable payloads')
  window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false } })
  await window.loadURL('data:text/html,<html><head></head><body><main id="root"><article class="setting-card" style="background:var(--surface)"><p>Text</p><p style="color:var(--muted)">Muted</p><div class="visuals-page"><header class="visuals-header">Header</header><input type="text" value="Input"><select><option>Option</option></select></div></article><div class="capability-scene-item">Scene</div><div class="capability-page-tabs"><button aria-selected="true">Tab</button></div></main></body></html>')
  const ui = expression => window.webContents.executeJavaScript(expression)
  const assets = path.join(root, 'electron/dist/renderer/assets')
  const css = fs.readdirSync(assets).filter(name => name.endsWith('.css')).map(name => fs.readFileSync(path.join(assets, name), 'utf8')).join('\n')
  await ui(`{ const style=document.createElement('style'); style.textContent=${JSON.stringify(css)}; document.head.appendChild(style); window.themeApplication=(${createThemeApplication.toString()})(document,URL); true }`)
  const colors = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/extension_theme_colors.json'), 'utf8'))
  for (const color of colors.accepted) {
    parseThemeColor(color)
    assert.equal(await ui(`CSS.supports('color',${JSON.stringify(color)})`), true, color)
  }
  for (const color of colors.rejected) assert.throws(() => parseThemeColor(color))
  report.checks.push('Canonical accepted color literals parse in both Host and real Chromium')
  await ui("themeApplication.apply({selected:'wallpaper-slice',base:'wallpaper-slice',reason:''})")
  const builtin = await ui("getComputedStyle(document.querySelector('.setting-card')).backgroundImage")
  assert.match(builtin, /linear-gradient/)
  const boundary = { id: 'boundary', contract: 'amadeus.ui.theme/v0', base: 'wallpaper-slice', tokens: {
    '--bg': '#000', '--surface': '#000', '--surface-alt': '#000', '--text': '#787878', '--muted': '#5c5c5c', '--faint': '#787878', '--accent': '#2FA996',
    ...Object.fromEntries(['--hover','--pressed','--selection-bg','--subtle-fill','--selected-fill','--focus-fill'].map(name => [name, '#0000'])),
  } }
  for (const definition of [boundary, { ...manifest.contributes.theme[0], background: { image: 'sample.png', fit: 'cover', dim: .8 } }]) {
    const palette = validateThemePalette(definition)
    const theme = { ...palette, id: `org.example.test/${definition.id}`, version: '1.0.0', revision: 1,
      ...(palette.background ? { image: { mime: 'image/png', base64: png.toString('base64') } } : {}) }
    await ui(`themeApplication.apply(${JSON.stringify({ selected: theme.id, base: theme.base, reason: '', theme })})`)
    const painted = await ui(`(() => {
      const selectors=['.setting-card','.capability-scene-item','.capability-page-tabs button','.visuals-header','.visuals-page input','.visuals-page select'];
      const rgb = text => text.match(/[\\d.]+/g).map(Number);
      const lum = rgb => rgb.slice(0,3).reduce((sum, x, i) => { x/=255; return sum + (x<=.04045 ? x/12.92 : ((x+.055)/1.055)**2.4)*[.2126,.7152,.0722][i] },0);
      const ratio = (a,b) => (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
      return selectors.map(selector=>{const node=document.querySelector(selector),style=getComputedStyle(node),bg=rgb(style.backgroundColor); return {selector,image:style.backgroundImage,alpha:bg[3]??1,contrast:ratio(lum(rgb(style.color)),lum(bg)),muted:ratio(lum(rgb(getComputedStyle(document.querySelector('.setting-card p+ p')).color)),lum(bg))};});
    })()`)
    for (const row of painted) {
      assert.equal(row.image, 'none', row.selector)
      assert.equal(row.alpha, 1, row.selector)
      assert.ok(row.contrast >= 4.5 && row.muted >= 3, JSON.stringify(row))
    }
  }
  await ui("themeApplication.apply({selected:'wallpaper-slice',base:'wallpaper-slice',reason:''})")
  assert.equal(await ui("getComputedStyle(document.querySelector('.setting-card')).backgroundImage"), builtin)
  report.checks.push('Actual built CSS honors opaque surfaces and text/muted contrast for boundary and translucent canvas palettes; built-in decoration restores')
  report.passed = true
} catch (error) { report.error = String(error.stack || error) }
finally {
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
  window?.destroy()
  app.exit(report.passed ? 0 : 1)
}

}
void run().catch(error => { console.error(error); app.exit(1) })
