import assert from 'node:assert/strict'
import {EventEmitter} from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import ts from 'typescript'
import {loadTypeScript} from './helpers/loadTypeScript.mjs'

const cleanup = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url))
const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('index.ts', source, ts.ScriptTarget.ES2022, true)
const functions = ['retainUnknownBackendCleanup', 'launchBackend', 'stopBackend'].map(name =>
  ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name).getText(ast)).join('\n')
const compiled = ts.transpileModule(functions, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText

function fixture(t, overrides = {}) {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-backend-launch-')))
  t.after(() => fs.rmSync(root, {recursive: true, force: true}))
  const file = path.join(root, 'backend-cleanup.json')
  const calls = {spawn: [], warnings: [], applied: 0, removed: 0, ready: 0}
  const deps = {
    ...cleanup,
    BACKEND_CLEANUP_STATE: file, BACKEND_INSTANCE_NONCE: 'synthetic-launch-nonce',
    PROJECT_ROOT: root, BACKEND_WS: 'ws://fixture', BACKEND_PORT: 12345, BACKEND_TOKEN: 'synthetic-token',
    scheduledExtensionUpdates: true, EXTENSIONS_ENV: 'AMADEUS_EXTENSION_LAUNCH',
    applicationLifecycle: {isQuitting: false}, app: {isPackaged: false}, devServer: null,
    backendHealthStatus: async () => 'absent', getPythonCommand: () => 'python',
    resolveBackendProjectRoot: async () => root,
    bundledDictionaryEnvironment: () => ({}), defaultMpsFallbackEnvironment: () => ({}),
    extensionStartupStatus: async () => ({}), requestBackendAction: async () => false,
    extensionInstaller: {
      cleanupStoppedBackend: async () => {calls.removed++},
      prepareLaunch: async () => ({pendingRevisions: {}, environment: {}, extensions: '{}'}),
      acceptLaunch: async () => true,
    },
    desktopSettings: {markApplied: () => {calls.applied++}, reportExtensionWarning: message => calls.warnings.push(message)},
    console: {log() {}, warn() {}, error() {}},
    process: {platform: 'win32', env: {AMADEUS_BACKEND_CLEANUP_LAUNCH: 'inherited-untrusted-value'}},
    spawn: (command, args, options) => {
      calls.spawn.push({command, args, options})
      return Object.assign(new EventEmitter(), {pid: 12345, exitCode: null, signalCode: null})
    },
    waitForBackendReady: async () => {calls.ready++},
    ...overrides,
  }
  const api = new Function(...Object.keys(deps), `
    let pythonProcess = null, backendOwned = false, backendStopping = null, windowsWallpaper = null;
    let extensionCleanupConfirmed = true, backendCleanupUnavailable = false;
    const backendCleanupLaunches = new WeakMap();
    ${compiled}
    return {launchBackend, stopBackend, state: () => ({extensionCleanupConfirmed, backendCleanupUnavailable, backendOwned})};
  `)(...Object.values(deps))
  return {root, file, calls, deps, ...api}
}

for (const damage of ['zero-filled', 'missing-index', 'registration-error', 'root-probe-error']) {
  test(`${damage} does not block core startup or grant cleanup authority`, async t => {
    const f = fixture(t, {
      ...(damage === 'registration-error' ? {prepareBackendCleanup: async () => {throw Error('write failed')}} : {}),
      ...(damage === 'root-probe-error' ? {resolveBackendProjectRoot: async () => {throw Error('probe timed out')}} : {}),
      stopOwnedBackend: async (_proc, _request, options) => {
        assert.equal(await options.verifyCleanup(), false)
        assert.equal(options.cleanupLaunch, undefined)
        return false
      },
    })
    if (damage === 'zero-filled') fs.writeFileSync(f.file, Buffer.alloc(128))
    if (damage === 'missing-index') fs.mkdirSync(`${f.file}.receipts`)
    await f.launchBackend()
    assert.equal(f.calls.spawn.length, 1)
    assert.equal(f.calls.applied, 1)
    assert.equal(f.calls.ready, 1)
    assert.equal(f.calls.spawn[0].options.env.AMADEUS_BACKEND_CLEANUP_LAUNCH, undefined)
    assert.equal(f.calls.spawn[0].options.env.AMADEUS_BACKEND_AUTH_MODE, 'required')
    assert.ok(f.calls.warnings.some(message => /tracking is unavailable/.test(message)))
    assert.equal(f.state().extensionCleanupConfirmed, false)
    assert.equal(f.state().backendCleanupUnavailable, true)
    assert.equal(await cleanup.verifyPreviousBackendCleanup(f.file), false, 'a fresh verifier must still fence maintenance')
    if (damage === 'zero-filled') assert.deepEqual(fs.readFileSync(f.file), Buffer.alloc(128), 'do not erase damaged evidence')
    await f.stopBackend()
    const removed = f.calls.removed
    await f.launchBackend()
    assert.equal(f.calls.removed, removed, 'a retry must not collect files after untracked launch')
  })
}

for (const health of ['ready', 'starting']) test(`ledger failure cannot reject a ${health} authenticated backend`, async t => {
  const f = fixture(t, {backendHealthStatus: async () => health})
  fs.writeFileSync(f.file, Buffer.alloc(128))
  await f.launchBackend()
  assert.equal(f.calls.spawn.length, 0)
  assert.equal(f.calls.ready, health === 'starting' ? 1 : 0)
  assert.equal(f.state().backendOwned, true)
  assert.equal(f.state().extensionCleanupConfirmed, false)
  assert.equal(await cleanup.verifyPreviousBackendCleanup(f.file), false)
})

test('a foreign backend is still rejected before cleanup fallback', async t => {
  const f = fixture(t, {backendHealthStatus: async () => 'foreign'})
  await assert.rejects(f.launchBackend(), /another backend/)
  assert.equal(f.calls.spawn.length, 0)
  assert.equal(f.calls.warnings.length, 0)
})

test('successful registration is bound to the same process used for forced shutdown', async t => {
  let stopped = false
  const f = fixture(t, {stopOwnedBackend: async (proc, _request, options) => {
    assert.equal(proc.pid, 12345)
    assert.equal(options.cleanupLaunch, f.calls.spawn[0].options.env.AMADEUS_BACKEND_CLEANUP_LAUNCH)
    assert.equal(await options.verifyCleanup(), false)
    stopped = true
    return false
  }})
  await f.launchBackend()
  const launch = JSON.parse(f.calls.spawn[0].options.env.AMADEUS_BACKEND_CLEANUP_LAUNCH)
  assert.equal(launch.owner.nonce, 'synthetic-launch-nonce')
  assert.equal(f.state().backendCleanupUnavailable, false)
  await f.stopBackend()
  assert.equal(stopped, true)
})

for (const platform of ['linux', 'darwin']) test(`${platform} startup does not probe or register Windows cleanup`, async t => {
  const f = fixture(t, {scheduledExtensionUpdates: false,
    resolveBackendProjectRoot: async () => assert.fail('Windows root probe'),
    prepareBackendCleanup: async () => assert.fail('Windows registration'),
  })
  await f.launchBackend()
  assert.equal(f.calls.applied, 1)
  assert.equal(f.calls.warnings.length, 0)
  assert.equal(fs.existsSync(f.file), false)
})
