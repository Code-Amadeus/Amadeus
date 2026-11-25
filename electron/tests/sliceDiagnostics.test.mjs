import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { wallpaperWindowPolicy } from '../src/main/wallpaperWindowPolicy.ts'
import { wallpaperShapeSender } from '../src/main/wallpaperCanvasLifecycle.ts'

// Run the production readiness functions and shape IPC against a fake window,
// without importing Electron's main entry point or moving the real desktop.
const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('index.ts', source, ts.ScriptTarget.ES2022, true)
const names = ['applyElectronSliceShape',
  'reconcileElectronSliceReadiness', 'suspendElectronSlicePlacement', 'resetElectronSliceRenderReadiness']
const functions = names.map(name => {
  const declaration = ast.statements.find(s => ts.isFunctionDeclaration(s) && s.name?.text === name)
  assert.ok(declaration, `Missing production readiness function: ${name}`)
  return declaration.getText(ast)
})
const registration = ast.statements.find(s => ts.isExpressionStatement(s) && ts.isCallExpression(s.expression)
  && s.expression.expression.getText(ast) === 'ipcMain.handle'
  && s.expression.arguments[0]?.text === 'electron-slice.set-shape')
assert.ok(registration, 'Missing production Slice shape IPC handler')
const call = registration.expression
const compiled = ts.transpileModule(functions.join('\n')
  + `\nconst commit = ${call.arguments[1].getText(ast)};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function fixture() {
  const window = {
    visible: false, ignored: true, shape: null, destroyed: false, webContents: {},
    isDestroyed() { return this.destroyed },
    getContentBounds: () => ({ width: 400, height: 300 }),
    isVisible() { return this.visible },
    showInactive() { this.visible = true },
    hide() { this.visible = false },
    setShape(regions) { this.shape = regions },
    setIgnoreMouseEvents(value) { this.ignored = value },
  }
  const api = new Function('window', 'console', 'wallpaperWindowPolicy', 'wallpaperShapeSender', 'process', `
    let electronSliceWindow = window, electronSliceShape = null, electronSlicePlacementReady = false;
    const electronCanvasLifecycle = { window: null };
    ${compiled}
    return { commit: regions => commit({sender: window.webContents}, regions),
      untrusted: regions => commit({sender: {}}, regions),
      ready: () => { electronSlicePlacementReady = true; reconcileElectronSliceReadiness(window) },
      suspend: () => suspendElectronSlicePlacement(window),
      reset: () => resetElectronSliceRenderReadiness(window) };
  `)(window, { log() {} },
    wallpaperWindowPolicy, wallpaperShapeSender, { platform: 'win32' })
  return { api, window }
}

test('trusted shape commits wait for placement and apply later geometry, including repeated commits', () => {
  const { api, window } = fixture()
  const first = [{ x: 10, y: 10, width: 50, height: 30 }]
  assert.equal(api.untrusted(first), false)
  assert.equal(api.commit(null), false)
  assert.equal(api.commit({ regions: first }), false)
  assert.equal(window.shape, null)
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)

  assert.equal(api.commit([]), true)
  assert.equal(window.shape, null)
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)
  api.ready()
  assert.deepEqual(window.shape, [])
  assert.equal(window.visible, true)
  assert.equal(window.ignored, true)

  assert.equal(api.commit(first), true)
  assert.deepEqual(window.shape, first)
  assert.equal(window.visible, true)
  assert.equal(window.ignored, false)
  // Native frame changes can discard the applied shape. The same renderer
  // geometry must still restore it when submitted again.
  window.shape = []
  window.ignored = true
  assert.equal(api.commit(first), true)
  assert.deepEqual(window.shape, first)
  assert.equal(window.ignored, false)

  const moved = [{ ...first[0], x: 80 }]
  assert.equal(api.commit(moved), true)
  assert.deepEqual(window.shape, moved)
  assert.equal(api.untrusted([]), false)
  assert.equal(api.commit(null), false)
  api.ready()
  assert.deepEqual(window.shape, moved)
  assert.equal(window.visible, true)
  assert.equal(window.ignored, false)

  assert.equal(api.commit([]), true)
  assert.deepEqual(window.shape, [])
  assert.equal(window.visible, true)
  assert.equal(window.ignored, true)
  window.destroyed = true
  assert.equal(api.commit(first), false)
  assert.deepEqual(window.shape, [])
  assert.equal(window.ignored, true)
})

test('placement failure hides the Slice and recovery reapplies the latest regions until renderer reset', () => {
  const { api, window } = fixture()
  const regions = [{ x: 10, y: 10, width: 50, height: 30 }]
  api.ready()
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)
  assert.equal(window.shape, null)
  assert.equal(api.commit(regions), true)
  assert.deepEqual(window.shape, regions)
  assert.equal(window.visible, true)
  assert.equal(window.ignored, false)

  api.suspend()
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)
  const moved = [{ ...regions[0], x: 80 }]
  assert.equal(api.commit(moved), true)
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)
  assert.deepEqual(window.shape, regions)

  window.shape = []
  api.ready()
  assert.equal(window.visible, true)
  assert.equal(window.ignored, false)
  assert.deepEqual(window.shape, moved)
  // Placement reconciliation must also restore the shape of a visible window.
  window.shape = []
  window.ignored = true
  api.ready()
  assert.equal(window.visible, true)
  assert.equal(window.ignored, false)
  assert.deepEqual(window.shape, moved)

  api.reset()
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)
  assert.deepEqual(window.shape, [])
  api.ready()
  assert.equal(window.visible, false)
  assert.equal(window.ignored, true)
  assert.deepEqual(window.shape, [])

  assert.equal(api.commit(regions), true)
  assert.equal(window.visible, true)
  assert.equal(window.ignored, false)
  assert.deepEqual(window.shape, regions)
})
