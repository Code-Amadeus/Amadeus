import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'
import { wallpaperWindowPolicy } from '../src/main/wallpaperWindowPolicy.ts'
import { syncElectronSliceHost } from '../src/renderer/wallpaperSlice.ts'

// Exercise the real main-process URL/normalization functions without starting Electron.
const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('index.ts', source, ts.ScriptTarget.ES2022, true)
const names = new Set(['normalizeLocalPort', 'normalizeWallpaperBridge', 'electronSliceUrl'])
const functions = ast.statements.filter(s => ts.isFunctionDeclaration(s) && names.has(s.name?.text)).map(s => s.getText(ast)).join('\n')
const compiled = ts.transpileModule(functions, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const { normalizeWallpaperBridge, electronSliceUrl } = new Function('electronSliceLayout', 'wallpaperWindowPolicy', 'process',
  compiled + '; return { normalizeWallpaperBridge, electronSliceUrl };')(
  { x:0, y:0, width:1, height:1 }, wallpaperWindowPolicy, { platform:'darwin' })

test('the backend opt-in reaches the macOS Pixi scene through renderer IPC and main-process normalization', async () => {
  const previous = globalThis.window
  try {
    for (const value of [undefined, false, true, 'false']) {
      let payload
      globalThis.window = { amadeus: { openElectronSlice: async next => { payload = next; return true } } }
      assert.equal(await syncElectronSliceHost({ sliceHost:'electron', assetPort:17778, bridgePort:17797,
        graphicsProfile:'power_saving', renderMaxFps:30, renderMaxResolution:1.5, renderTextureSampling:value }), true)
      const bridge = normalizeWallpaperBridge(payload)
      assert.equal(bridge.renderTextureSampling, value === true)
      const url = new URL(electronSliceUrl(bridge))
      assert.equal(url.pathname, '/render/web/wallpaper_engine.html')
      assert.equal(url.searchParams.get('renderTextureSampling'), value === true ? '1' : '0')
    }
  } finally { globalThis.window = previous }
})
