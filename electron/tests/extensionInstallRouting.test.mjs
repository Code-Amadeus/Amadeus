import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import {loadTypeScript} from './helpers/loadTypeScript.mjs'

const {usesScheduledExtensionUpdates} = loadTypeScript(new URL('../src/shared/extensionCatalog.ts', import.meta.url))
const {ExtensionUpdateBoundary} = loadTypeScript(new URL('../src/main/extensionUpdates.ts', import.meta.url))
const source = ts.createSourceFile('index.ts', fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)

// Execute the production IPC entry points without launching Electron or a
// backend. Their delivery boundary must agree with the renderer's platform.
function handlersFor(platform) {
  const handlers = new Map(), calls = []
  const statements = source.statements.filter(statement => {
    if (!ts.isExpressionStatement(statement) || !ts.isCallExpression(statement.expression)) return false
    const call = statement.expression
    return call.expression.getText(source) === 'ipcMain.handle'
      && ['extensions.install', 'extensions.catalog'].includes(call.arguments[0]?.text)
  })
  assert.equal(statements.length, 2)
  const context = {
    ipcMain: {handle: (name, callback) => handlers.set(name, callback)},
    scheduledExtensionUpdates: usesScheduledExtensionUpdates(platform),
    extensionUpdateBoundary: new ExtensionUpdateBoundary(async () => false),
    extensionInstaller: {install: async file => {calls.push(['install-local', file]); return {installed: true}}},
    extensionCatalog: {
      scheduleLocal: async file => {calls.push(['schedule-local', file])},
      dispatch: async request => {calls.push(['catalog', request.action]); return {ok: true, settings: {installed: true}}},
    },
    isTrustedBackendRenderer: sender => sender === 'trusted',
    dialog: {showOpenDialog: async () => ({canceled: false, filePaths: ['selected-wheel.whl']})},
    mainWindow: undefined, backendOwned: false, process: {env: {}},
    shell: {openExternal: async () => {}}, ExtensionChangeCancelled: class extends Error {},
  }
  const code = ts.transpileModule(statements.map(statement => statement.getText(source)).join('\n'), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText
  new Function(...Object.keys(context), code)(...Object.values(context))
  return {handlers, calls}
}

for (const platform of ['linux', 'darwin']) {
  test(`${platform} native and catalog IPC use immediate delivery even while backend maintenance is unavailable`, async () => {
    const {handlers, calls} = handlersFor(platform)
    const local = await handlers.get('extensions.install')({sender: 'trusted'})
    assert.deepEqual(local, {ok: true, settings: {installed: true}})
    assert.equal((await handlers.get('extensions.catalog')({sender: 'trusted'}, {action: 'install', id: 'org.example.demo', version: '1.0.0'})).ok, true)
    assert.deepEqual(calls, [['install-local', 'selected-wheel.whl'], ['catalog', 'install']])
  })
}

test('Windows native IPC queues installation and direct catalog installation still requires cleanup proof', async () => {
  const {handlers, calls} = handlersFor('win32')
  assert.deepEqual(await handlers.get('extensions.install')({sender: 'trusted'}), {ok: true, scheduled: true})
  const blocked = await handlers.get('extensions.catalog')({sender: 'trusted'}, {action: 'install', id: 'org.example.demo', version: '1.0.0'})
  assert.equal(blocked.ok, false)
  assert.match(blocked.error, /active or unconfirmed/)
  assert.deepEqual(calls, [['schedule-local', 'selected-wheel.whl']])
})

test('platform routing does not bypass trusted renderer admission', async () => {
  for (const platform of ['win32', 'linux', 'darwin']) {
    const {handlers, calls} = handlersFor(platform)
    assert.equal((await handlers.get('extensions.install')({sender: 'foreign'})).ok, false)
    assert.equal((await handlers.get('extensions.catalog')({sender: 'foreign'}, {action: 'install'})).ok, false)
    assert.deepEqual(calls, [])
  }
})
