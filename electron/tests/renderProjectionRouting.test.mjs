import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// Run the actual App toggle callbacks without native windows or React mounting.
const source = fs.readFileSync(new URL('../src/renderer/App.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX)
const app = ast.statements.find(statement => ts.isFunctionDeclaration(statement)
  && statement.name?.text === 'AmadeusApp')

function callback(name, bindings) {
  const declaration = app.body.statements.filter(ts.isVariableStatement)
    .flatMap(statement => [...statement.declarationList.declarations])
    .find(declaration => declaration.name.getText(ast) === name)
  const compiled = ts.transpileModule(`const handler = ${declaration.initializer.arguments[0].getText(ast)}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText
  return new Function(...Object.keys(bindings), `${compiled}; return handler;`)(...Object.values(bindings))
}

function desktop({ renderActive = false, wallpaperActive = false, wallpaperFails = false, renderStarts = [], stops = [] } = {}) {
  const state = { renderActive, wallpaperActive, backend: 'graph', renderAssetUrl: '', page: 'chat' }
  const calls = []
  const projectionRequestRef = { current: 0 }
  const send = async (method, params) => {
    calls.push(method)
    if (method === 'expression.set_backend') state.backend = params.backend
    if (method === 'wallpaper.start') return { status: wallpaperFails ? 'error' : 'started' }
    if (method === 'render.start') {
      const result = renderStarts.length ? renderStarts.shift() : { url: 'render.html' }
      if (result instanceof Error) throw result
      return result
    }
    return stops.length ? stops.shift() : { status: 'stopped' }
  }
  return {
    state, calls,
    async toggle(surface) {
      await callback(surface === 'render' ? 'handleToggleRender' : 'handleToggleWallpaper', {
        renderActive: state.renderActive,
        wallpaperActive: state.wallpaperActive,
        send, projectionRequestRef,
        setRenderActive: value => { state.renderActive = value },
        setWallpaperActive: value => { state.wallpaperActive = value },
        setRenderAssetUrl: value => { state.renderAssetUrl = value },
        setPage: value => { state.page = value },
        stopElectronSliceHost: async send => { await send('wallpaper.stop', {}); return true },
        syncElectronSliceHost: async () => true,
        ELECTRON_SLICE_START_PARAMS: { slice_host: 'electron' },
      })()
    },
  }
}

for (const sequence of [
  ['wallpaper'],
  ['render', 'wallpaper'],
  ['render', 'render', 'wallpaper'],
  ['wallpaper', 'render', 'wallpaper'],
]) {
  test(`${sequence.join(' → ')} preserves the shared speech/expression route`, async () => {
    const app = desktop()
    for (const surface of sequence) await app.toggle(surface)
    assert.equal(app.state.wallpaperActive, true)
    assert.equal(app.state.renderActive, false)
    assert.equal(app.state.backend, 'graph', 'wallpaper must still receive speech and EMO signals')
    assert.ok(app.calls.includes('wallpaper.start'))
    assert.ok(!app.calls.includes('expression.set_backend'), 'projection visibility does not own expression routing')
  })
}

test('closing or failing to open a projection preserves the shared signal route', async () => {
  for (const surface of ['render', 'wallpaper']) {
    const app = desktop({ renderActive: surface === 'render', wallpaperActive: surface === 'wallpaper' })
    await app.toggle(surface)
    assert.equal(app.state.renderActive, false)
    assert.equal(app.state.wallpaperActive, false)
    assert.equal(app.state.backend, 'graph')
  }
  const app = desktop({ renderActive: true, wallpaperFails: true })
  await app.toggle('wallpaper')
  assert.equal(app.state.wallpaperActive, false)
  assert.equal(app.state.backend, 'graph')
})


test('failed Render starts close the empty projection and allow a later connected start', async () => {
  for (const failure of [new Error('not connected'), new Error('start failed'), {}, { status: 'error' }, { url: '' }]) {
    const app = desktop({ renderStarts: [failure] })
    await app.toggle('render')
    assert.equal(app.state.renderActive, false, 'a failed start must not leave an empty active iframe')
    assert.equal(app.state.renderAssetUrl, '')
    assert.equal(app.state.backend, 'graph')
    await app.toggle('render')
    assert.equal(app.state.renderActive, true)
    assert.equal(app.state.renderAssetUrl, 'render.html')
    assert.equal(app.state.backend, 'graph')
    assert.deepEqual(app.calls, ['render.start', 'render.start'])
  }
})


function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('a pending Render start cannot reopen a projection closed before its reply', async () => {
  const start = deferred(), stop = deferred()
  const app = desktop({ renderStarts: [start.promise], stops: [stop.promise] })
  const opening = app.toggle('render')
  await app.toggle('render')
  assert.equal(app.state.renderActive, false)
  // Backend replies remain FIFO: start completes before stop.
  start.resolve({ url: 'old.html' }); await opening
  assert.equal(app.state.renderActive, false)
  assert.equal(app.state.renderAssetUrl, '')
  stop.resolve({ status: 'stopped' })
})

test('old Render success and failure cannot overwrite a newer open intent', async () => {
  for (const failed of [false, true]) {
    const first = deferred(), second = deferred(), stop = deferred()
    const app = desktop({ renderStarts: [first.promise, second.promise], stops: [stop.promise] })
    const opening = app.toggle('render')
    await app.toggle('render')
    const reopening = app.toggle('render')
    if (failed) first.reject(new Error('old start failed'))
    else first.resolve({ url: 'old.html' })
    await opening
    assert.equal(app.state.renderActive, true, 'the new open intent survives the old reply')
    assert.equal(app.state.renderAssetUrl, '', 'the old URL must not mount an iframe')
    stop.resolve({ status: 'stopped' })
    second.resolve({ url: 'new.html' }); await reopening
    assert.equal(app.state.renderActive, true)
    assert.equal(app.state.renderAssetUrl, 'new.html')
  }
})

test('switching to Wallpaper invalidates pending Render results before backend stop completes', async () => {
  const start = deferred(), stop = deferred()
  const app = desktop({ renderStarts: [start.promise], stops: [stop.promise] })
  const opening = app.toggle('render')
  const wallpaper = app.toggle('wallpaper')
  start.resolve({ url: 'old.html' }); await opening
  assert.equal(app.state.renderActive, false)
  assert.equal(app.state.renderAssetUrl, '')
  stop.resolve({ status: 'stopped' }); await wallpaper
  assert.equal(app.state.wallpaperActive, true)
  assert.deepEqual(app.calls, ['render.start', 'render.stop', 'wallpaper.start'])
})


test('a Render start waiting for Wallpaper to stop is cancelled by a later close', async () => {
  const stop = deferred()
  const app = desktop({ wallpaperActive: true, stops: [stop.promise] })
  const opening = app.toggle('render')
  await app.toggle('render')
  stop.resolve({ status: 'stopped' }); await opening
  assert.equal(app.state.renderActive, false)
  assert.equal(app.state.renderAssetUrl, '')
  assert.ok(!app.calls.includes('render.start'))
})

test('an older Wallpaper switch cannot start after a newer Render intent', async () => {
  const stop = deferred()
  const app = desktop({ renderActive: true, stops: [stop.promise] })
  const wallpaper = app.toggle('wallpaper')
  const render = app.toggle('render')
  stop.resolve({ status: 'stopped' })
  await Promise.all([wallpaper, render])
  assert.equal(app.state.renderActive, true)
  assert.equal(app.state.wallpaperActive, false)
  assert.equal(app.state.renderAssetUrl, 'render.html')
  assert.ok(!app.calls.includes('wallpaper.start'))
})
