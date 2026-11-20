import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'
import ts from 'typescript'

const require = createSourceRequire(new URL('../src/main/desktopSettings.ts', import.meta.url))
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: {
      isEncryptionAvailable: () => true,
      encryptString: value => Buffer.from(`encrypted:${value}`),
      decryptString: buffer => buffer.toString().slice(10),
    } }
  : require(name), exports)

function savedStore(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-settings-durability-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'settings.json')
  const store = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
  store.update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' }, secrets: { DEEPSEEK_API_KEY: 'sk-test' } })
  return { root, file, store }
}

// A power loss during a write can leave a full-length file of NUL bytes.
function zeroFill(file) {
  fs.writeFileSync(file, Buffer.alloc(fs.statSync(file).size))
}

test('a damaged settings file is restored from its last save instead of reset by the next launch', t => {
  const { file, store } = savedStore(t)
  zeroFill(file)

  store.markApplied({}, {})

  const restored = JSON.parse(fs.readFileSync(file, 'utf8'))
  assert.equal(restored.values.WORK_EXECUTION_PROVIDER, 'pi')
  assert.ok(restored.encryptedSecrets.DEEPSEEK_API_KEY)
  assert.equal(store.backendEnvironment({}).DEEPSEEK_API_KEY, 'sk-test')
})

test('unreadable settings without a saved copy are set aside, not overwritten', t => {
  const { root, file, store } = savedStore(t)
  fs.rmSync(`${file}.bak`)
  zeroFill(file)
  const damaged = fs.readFileSync(file)

  store.markApplied({}, {})

  const setAside = fs.readdirSync(root).filter(name => name.startsWith('settings.json.corrupt-'))
  assert.equal(setAside.length, 1)
  assert.deepEqual(fs.readFileSync(path.join(root, setAside[0])), damaged)
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).values, {})
})

test('settings that cannot be opened are never replaced with defaults', t => {
  const { file, store } = savedStore(t)
  fs.rmSync(`${file}.bak`)
  fs.rmSync(file)
  fs.mkdirSync(file)

  assert.throws(() => store.markApplied({}, {}))
  assert.ok(fs.statSync(file).isDirectory())
})

test('deleting the settings file still resets to defaults', t => {
  const { file, store } = savedStore(t)
  fs.rmSync(file)

  assert.equal(store.backendEnvironment({}).WORK_EXECUTION_PROVIDER, undefined)
  store.markApplied({}, {})

  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).values, {})
  assert.deepEqual(JSON.parse(fs.readFileSync(`${file}.bak`, 'utf8')).values, {})
})

test('a first launch without settings still starts from defaults', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-settings-fresh-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const store = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))

  assert.deepEqual(store.pendingRevisionSnapshot(), {})
  store.markApplied({}, {})

  assert.deepEqual(fs.readdirSync(root).sort(), ['settings.json', 'settings.json.bak'])
})

test('legacy manual MCP bindings survive explicit encrypted environment migration and restart', t => {
  const { file, store, root } = savedStore(t)
  const connection = { id: 'legacy', name: 'Legacy manual MCP', transport: 'stdio', command: 'python',
    arguments: ['fixture.py'], cwd: root, enabled: true, providerIds: ['pi'] }
  store.upsertMcpConnection({}, { connection })
  const old = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
  const initial = JSON.parse(old.backendEnvironment({ FIXTURE_TOKEN: 'ambient-only' }).AMADEUS_MCP_CONNECTIONS)
  assert.deepEqual(initial.connections[0].environment, {})
  old.upsertMcpConnection({}, { connection, environment: { FIXTURE_TOKEN: 'synthetic-explicit-token' } })
  assert.ok(!fs.readFileSync(file, 'utf8').includes('synthetic-explicit-token'))
  const restarted = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
  const registry = JSON.parse(restarted.backendEnvironment({}).AMADEUS_MCP_CONNECTIONS)
  assert.equal(registry.connections[0].environment.FIXTURE_TOKEN, 'synthetic-explicit-token')
  assert.deepEqual(registry.connections[0].provider_ids, ['pi'])
  assert.equal(registry.connections[0].command, 'python')
  assert.ok(!JSON.stringify(restarted.snapshot({})).includes('synthetic-explicit-token'))
})

const { securitySnapshot } = require('./extensionSecurity.js')
const identity = { id: 'org.example.demo', version: '0.1.0', digest: 'a'.repeat(64) }
const facts = (sequence, blocked = true) => securitySnapshot(sequence, [{ ...identity, blocked, sequence }], '2026-10-21T00:00:00Z')
function restart(file) { return new exports.DesktopSettingsStore(file, path.join(path.dirname(file), '.env')) }

for (const damaged of ['primary', 'backup', 'missing-primary']) test(`security survives ${damaged} loss and ordinary save`, t => {
  const { file, store } = savedStore(t)
  store.commitExtensionSecurity(facts(5))
  if (damaged === 'missing-primary') fs.unlinkSync(file)
  else zeroFill(damaged === 'primary' ? file : file + '.bak')
  const next = restart(file)
  assert.match(next.extensionSecurityBlock(identity), /blocked/)
  next.update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' } })
  assert.equal(restart(file).extensionSecurityState().sequence, 5)
  assert.match(restart(file).extensionSecurityBlock(identity), /blocked/)
})
for (const damage of ['nul', 'truncated', 'field']) test(`both copies ${damage}: durable fail-closed until explicit official recovery`, t => {
  const { root, file, store } = savedStore(t)
  store.commitExtensionSecurity(facts(5))
  for (const target of [file, file + '.bak']) {
    if (damage === 'nul') zeroFill(target)
    else if (damage === 'truncated') fs.writeFileSync(target, '{"version":2,"extensionSecurity":')
    else { const raw = JSON.parse(fs.readFileSync(target)); raw.extensionSecurity.decisions = []; fs.writeFileSync(target, JSON.stringify(raw)) }
  }
  let next = restart(file)
  assert.equal(next.extensionSecurityState().status, 'unavailable')
  assert.match(next.extensionSecurityBlock(), /damaged/)
  assert.equal(fs.readdirSync(root).filter(n => n.includes('.corrupt-')).length, 2)
  next.update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' } })
  next.markApplied({}, {})
  next = restart(file)
  assert.equal(next.extensionSecurityState().status, 'unavailable')
  assert.throws(() => next.commitExtensionSecurity(facts(6)), /explicit/)
  next.commitExtensionSecurity(facts(6), true)
  assert.match(restart(file).extensionSecurityBlock(identity), /blocked/)
  next.commitExtensionSecurity(facts(7, false))
  assert.equal(restart(file).extensionSecurityBlock(identity), '')
})
test('higher complete backup survives failure between backup and primary commit', t => {
  const { file, store } = savedStore(t)
  store.commitExtensionSecurity(facts(3, false))
  const rename = fs.renameSync
  fs.renameSync = (source, target) => { if (target === file) throw new Error('injected power loss'); return rename(source, target) }
  try { assert.throws(() => store.commitExtensionSecurity(facts(4)), /power loss/) } finally { fs.renameSync = rename }
  assert.equal(JSON.parse(fs.readFileSync(file)).extensionSecurity.sequence, 3)
  assert.equal(JSON.parse(fs.readFileSync(file + '.bak')).extensionSecurity.sequence, 4)
  assert.match(restart(file).extensionSecurityBlock(identity), /blocked/)
  restart(file).update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' } })
  assert.equal(JSON.parse(fs.readFileSync(file)).extensionSecurity.sequence, 4)
})
test('same-sequence conflict stays unavailable across interrupted fault persistence and restart', t => {
  const { file, store } = savedStore(t)
  store.commitExtensionSecurity(facts(4))
  const backup = JSON.parse(fs.readFileSync(file + '.bak'))
  backup.extensionSecurity = facts(4, false)
  fs.writeFileSync(file + '.bak', JSON.stringify(backup))
  const rename = fs.renameSync
  fs.renameSync = (source, target) => { if (target === file) throw new Error('injected power loss'); return rename(source, target) }
  try { assert.throws(() => restart(file).extensionSecurityState(), /power loss/) } finally { fs.renameSync = rename }
  const next = restart(file)
  assert.equal(next.extensionSecurityState().status, 'unavailable')
  next.update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' } })
  assert.equal(restart(file).extensionSecurityState().status, 'unavailable')
  assert.throws(() => next.commitExtensionSecurity(facts(3), true), /rollback/)
  next.commitExtensionSecurity(facts(4), true)
  assert.match(restart(file).extensionSecurityBlock(identity), /blocked/)
})
test('legacy migration, replay, omission, same-sequence change and explicit higher unban', t => {
  const { file, store } = savedStore(t)
  assert.equal(store.extensionSecurityState(), undefined)
  store.commitExtensionSecurity(securitySnapshot(0, [], '2026-10-21T00:00:00Z'))
  store.commitExtensionSecurity(facts(3))
  assert.throws(() => store.commitExtensionSecurity(facts(2)), /rollback/)
  assert.throws(() => store.commitExtensionSecurity(facts(3, false)), /Conflicting/)
  assert.throws(() => store.commitExtensionSecurity(securitySnapshot(4, [], '2026-10-21T00:00:00Z')), /disappear/)
  store.commitExtensionSecurity(securitySnapshot(4, [{ ...identity, blocked: true, sequence: 3 }], '2026-10-21T00:00:00Z'))
  assert.match(restart(file).extensionSecurityBlock(identity), /blocked/)
  store.commitExtensionSecurity(facts(6, false))
  assert.equal(restart(file).extensionSecurityBlock(identity), '')
  assert.equal(store.backendEnvironment({}).DEEPSEEK_API_KEY, 'sk-test')
})
