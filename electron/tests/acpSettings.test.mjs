import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => true, encryptString: value => Buffer.from(`encrypted:${value}`), decryptString: buffer => buffer.toString().slice(10) } }
  : require(name), exports)

const profile = { id: 'deepseek', name: 'DeepSeek Harness', command: 'node', args: ['C:/agent/bin.js', '--profile', 'acp'],
  enabled: true, resume: true, environment: { DEEPSEEK_API_KEY: 'DEEPSEEK_API_KEY' }, config_options: { model: 'chosen-model' } }

test('ACP profiles survive restart, keep credentials separate and allow explicit MCP bindings', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-acp-settings-'))
  try {
    const file = path.join(root, 'settings.json')
    const store = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
    store.update({}, { values: { AMADEUS_ACP_PROVIDERS: JSON.stringify([profile]) }, secrets: { ANTHROPIC_API_KEY: 'test-credential' } })
    const loaded = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
    assert.deepEqual(JSON.parse(loaded.backendEnvironment({}).AMADEUS_ACP_PROVIDERS), [profile])
    assert.equal(loaded.backendEnvironment({}).ANTHROPIC_API_KEY, 'test-credential')
    assert.ok(!JSON.stringify(loaded.snapshot({})).includes('test-credential'))
    loaded.upsertMcpConnection({}, { connection: { id: 'files', name: 'Files', enabled: true,
      transport: 'stdio', command: 'node', arguments: ['C:/mcp.js'], providerIds: ['deepseek'] } })
    const reloaded = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
    assert.deepEqual(reloaded.snapshot({}).mcpConnections[0].providerIds, ['deepseek'])
    assert.throws(() => loaded.upsertMcpConnection({}, { connection: { id: 'wrong', name: 'Wrong', enabled: true,
      transport: 'stdio', command: 'node', providerIds: ['invalid provider id'] } }))
    assert.throws(() => loaded.update({ AMADEUS_ACP_PROVIDERS: '[]' }, { values: { AMADEUS_ACP_PROVIDERS: JSON.stringify([profile]) } }))
    assert.equal(loaded.backendEnvironment({ AMADEUS_ACP_PROVIDERS: '[]' }).AMADEUS_ACP_PROVIDERS, undefined)
  } finally {
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()))
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('ACP configuration rejects duplicate identities, built-in collisions and inline secrets', () => {
  for (const profiles of [[profile, profile], [{ ...profile, id: 'codex' }], [{ ...profile, environment: { KEY: 'sk-not-a-reference' } }],
    [{ ...profile, enabled: 'false' }], [{ ...profile, config_options: { model: true } }], [{ ...profile, extra: 'unknown' }], [null]]) {
    assert.throws(() => exports.validateAcpProviders(JSON.stringify(profiles)))
  }
  assert.deepEqual(exports.validateAcpProviders('[]'), [])
})
