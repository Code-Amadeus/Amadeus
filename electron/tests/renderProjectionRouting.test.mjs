import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'
import { createProjectionController } from '../src/renderer/projectionController.ts'

const flush = () => new Promise(resolve => setImmediate(resolve))
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function desktop({ starts = [], renders = [], stops = [], mounts = [] } = {}) {
  const calls = [], errors = []
  let host = false, native = false, controller
  controller = createProjectionController({
    send: async method => {
      calls.push(method)
      if (method === 'wallpaper.start') {
        const result = starts.length ? await starts.shift() : { status: 'started' }
        if (result.status !== 'error') host = true
        return result
      }
      if (method === 'render.start') return renders.length ? await renders.shift() : { url: 'render.html' }
      return { status: 'stopped' }
    },
    stopWallpaper: async () => {
      calls.push('wallpaper.stop')
      const stopped = stops.length ? await stops.shift() : true
      if (stopped) { host = false; native = false; controller.exited() }
      return stopped
    },
    openWallpaper: async () => { calls.push('native.open'); if (mounts.length) await mounts.shift(); native = true },
    closeWallpaper: async () => { calls.push('native.close'); native = false },
    onError: error => errors.push(error),
  })
  return { ...controller, calls, errors, state: controller.getSnapshot,
    ready() { host = true; controller.ready({ status: 'started' }) },
    exited() { host = false; controller.exited() },
    host: () => host, native: () => native }
}

for (const ready of [false, true]) {
  test(`automatic startup is stopped before a later Render opens (ready=${ready})`, async () => {
    const start = deferred(), stop = deferred()
    const app = desktop({ starts: [start.promise], stops: [stop.promise] })
    const automatic = app.startAutomatically(); await flush()
    const render = app.toggle('render')
    if (ready) app.ready()
    start.resolve({ status: 'started' }); await automatic; await flush()
    assert.deepEqual(app.calls, ['wallpaper.start', 'wallpaper.stop'])
    assert.equal(app.native(), false, 'superseded automatic start never mounts a native window')
    stop.resolve(true); await render
    assert.deepEqual(app.state(), { renderActive: true, wallpaperActive: false, renderAssetUrl: 'render.html' })
    assert.equal(app.host(), false)
    assert.equal(app.native(), false)
  })
}

test('automatic startup never replaces an earlier manual choice', async () => {
  const app = desktop()
  const render = app.toggle('render')
  assert.equal((await app.startAutomatically()).status, 'superseded')
  await render
  assert.deepEqual(app.calls, ['render.start'])
})

for (const failure of [{ status: 'error', error: 'unavailable' }, new Error('transport closed')]) {
  test(`automatic start failure cannot roll back a newer Render choice: ${String(failure.error || failure)}`, async () => {
    const start = deferred(), app = desktop({ starts: [start.promise] })
    const automatic = app.startAutomatically()
    const rejected = assert.rejects(automatic)
    await flush()
    const render = app.toggle('render')
    if (failure instanceof Error) start.reject(failure)
    else start.resolve(failure)
    await rejected; await render
    assert.equal(app.state().renderActive, true)
    assert.equal(app.state().wallpaperActive, false)
  })
}

test('ready followed by a failed start response still leaves a Host to clean up before Render', async () => {
  const start = deferred(), app = desktop({ starts: [start.promise] })
  const failed = assert.rejects(app.startAutomatically())
  await flush(); app.ready()
  const render = app.toggle('render')
  start.reject(new Error('reply lost')); await failed; await render
  assert.deepEqual(app.calls, ['wallpaper.start', 'wallpaper.stop', 'render.start'])
  assert.equal(app.host(), false)
})

test('a late native mount completes before cleanup and Render start', async () => {
  const mount = deferred(), app = desktop({ mounts: [mount.promise] })
  const automatic = app.startAutomatically(); await flush()
  const render = app.toggle('render'); await flush()
  assert.deepEqual(app.calls, ['wallpaper.start', 'native.open'])
  mount.resolve(); await automatic; await render
  assert.deepEqual(app.calls, ['wallpaper.start', 'native.open', 'wallpaper.stop', 'render.start'])
  assert.equal(app.native(), false)
})

test('rapid Render open, close and reopen never mounts the superseded URL', async () => {
  const start = deferred(), app = desktop({ renders: [start.promise] })
  const first = app.toggle('render'); await flush()
  const close = app.toggle('render')
  const reopen = app.toggle('render')
  start.resolve({ url: 'render.html' }); await Promise.all([first, close, reopen])
  assert.equal(app.state().renderActive, true)
  assert.equal(app.state().renderAssetUrl, 'render.html')
})

test('Render close while start is pending cleans up the completed start', async () => {
  const start = deferred(), app = desktop({ renders: [start.promise] })
  const first = app.toggle('render'); await flush()
  const close = app.toggle('render')
  start.resolve({ url: 'old.html' }); await Promise.all([first, close])
  assert.deepEqual(app.state(), { renderActive: false, wallpaperActive: false, renderAssetUrl: '' })
  assert.deepEqual(app.calls, ['render.start', 'render.stop'])
})

for (const response of [{}, { url: '' }, { status: 'error' }]) {
  test(`failed Render result ${JSON.stringify(response)} never leaves an empty iframe and permits retry`, async () => {
    const app = desktop({ renders: [response] })
    await assert.rejects(app.start('render'))
    assert.equal(app.state().renderActive, false)
    await app.start('render')
    assert.equal(app.state().renderAssetUrl, 'render.html')
  })
}

test('failed Wallpaper stop preserves its running state and blocks Render, then allows an explicit retry', async () => {
  const app = desktop({ stops: [false] })
  await app.start('wallpaper')
  await assert.rejects(app.start('render'), /could not be stopped/)
  assert.equal(app.state().wallpaperActive, true)
  assert.equal(app.state().renderActive, false)
  assert.equal(app.calls.includes('render.start'), false)
  await app.start('render')
  assert.equal(app.state().renderActive, true)
  assert.equal(app.host(), false)
})

test('cleanup acknowledgement is awaited before reopening Wallpaper', async () => {
  const stop = deferred(), app = desktop({ stops: [stop.promise] })
  await app.start('wallpaper')
  const render = app.start('render'); await flush()
  const reopen = app.start('wallpaper'); await flush()
  assert.equal(app.calls.filter(call => call === 'wallpaper.start').length, 1)
  stop.resolve(true); await Promise.all([render, reopen])
  assert.equal(app.state().wallpaperActive, true)
  assert.equal(app.state().renderActive, false)
  assert.equal(app.native(), true)
  assert.equal(app.calls.filter(call => call === 'wallpaper.start').length, 2)
})

test('cancelled switch records stop success and does not start Render', async () => {
  const stop = deferred(), app = desktop({ stops: [stop.promise] })
  await app.start('wallpaper')
  const render = app.toggle('render'); await flush()
  const cancel = app.toggle('render')
  stop.resolve(true); await Promise.all([render, cancel])
  assert.equal(app.state().wallpaperActive, false)
  assert.equal(app.state().renderActive, false)
  assert.equal(app.calls.includes('render.start'), false)
})

test('external ready events remain supported and serialize against existing Render', async () => {
  const app = desktop()
  await app.start('render')
  app.ready(); await flush()
  assert.equal(app.state().wallpaperActive, true)
  assert.equal(app.state().renderActive, false)
  assert.equal(app.native(), true)
  assert.deepEqual(app.calls, ['render.start', 'render.stop', 'wallpaper.start', 'native.open'])
})

test('external exit closes native state without another stop RPC loop', async () => {
  const app = desktop()
  await app.start('wallpaper')
  app.exited(); await flush()
  assert.equal(app.state().wallpaperActive, false)
  assert.equal(app.native(), false)
  assert.equal(app.calls.includes('wallpaper.stop'), false)
})

test('normal ready/start response/exit/stop response ordering supports fast Wallpaper to Render switching', async () => {
  const start = deferred(), stop = deferred(), app = desktop({ starts: [start.promise], stops: [stop.promise] })
  const wallpaper = app.start('wallpaper'); await flush()
  const render = app.start('render')
  app.ready(); start.resolve({ status: 'started' }); await wallpaper; await flush()
  app.exited(); stop.resolve(true); await render
  assert.equal(app.state().renderActive, true)
  assert.equal(app.state().wallpaperActive, false)
})

test('projection ownership never changes the shared expression route', async () => {
  const app = desktop()
  for (const mode of ['render', 'wallpaper', 'render', 'wallpaper']) await app.start(mode)
  await app.stop('wallpaper')
  assert.ok(app.calls.every(call => call !== 'expression.set_backend'))
})


function productionCallback(file, component, name) {
  const source = fs.readFileSync(new URL(file, import.meta.url), 'utf8')
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX)
  const fn = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === component)
  const variable = fn.body.statements.filter(ts.isVariableStatement).flatMap(node => [...node.declarationList.declarations])
    .find(node => node.name.getText(ast) === name)
  return variable.initializer.arguments[0].getText(ast)
}
function bindCallback(source, bindings) {
  const code = ts.transpileModule(`return (${source})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return new Function(...Object.keys(bindings), code)(...Object.values(bindings))
}

test('App sidebar callbacks use the same owner as automatic startup', async () => {
  const app = desktop(), pages = []
  for (const [name, expected] of [['handleToggleRender', 'render'], ['handleToggleWallpaper', 'wallpaper']]) {
    const fn = bindCallback(productionCallback('../src/renderer/App.tsx', 'AmadeusApp', name), {
      projections: app, setPage: page => pages.push(page), console,
    })
    fn(); await flush()
    assert.equal(app.state()[`${expected}Active`], true)
  }
  assert.deepEqual(pages, ['chat', 'chat'])
  assert.equal((await app.startAutomatically()).status, 'superseded')
})

test('Backend controls route all four explicit actions through the projection owner', async () => {
  const calls = [], feedback = []
  const fn = bindCallback(productionCallback('../src/renderer/components/BackendPage.tsx', 'BackendPage', 'doAction'), {
    projections: {
      start: async mode => { calls.push(`start:${mode}`); return { status: 'started' } },
      stop: async mode => { calls.push(`stop:${mode}`); return { status: 'stopped' } },
    },
    send: () => assert.fail('Backend control must not bypass lifecycle ownership'),
    setActionsDisabled() {}, fetchLog() {},
  })
  for (const mode of ['render', 'wallpaper']) {
    await fn(mode, true, value => feedback.push(value))
    await fn(mode, false, value => feedback.push(value))
  }
  assert.deepEqual(calls, ['start:render', 'stop:render', 'start:wallpaper', 'stop:wallpaper'])
  assert.ok(feedback.includes('{"status":"started"}'))
})
