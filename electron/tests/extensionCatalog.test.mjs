import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
const mocks = { electron: { safeStorage: { isEncryptionAvailable: () => true } } }
const { ExtensionCatalog, catalogRequest, OFFICIAL_EXTENSION_INDEX } = loadTypeScript(new URL('../src/main/extensionCatalog.ts', import.meta.url), mocks)
const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), mocks)
const entry = () => ({ id: 'org.example.demo', version: '0.1.0', sha256: 'a'.repeat(64), url: 'https://source.example/demo-0.1.0-py3-none-any.whl', size: 5, display: { homepage: 'https://example.org/' }, compatibility: {}, publisher: {}, review: {}, withdrawal: null, incompatible: [] })
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-catalog-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const store = new DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))
  const state = { index: { format: 'amadeus.catalog/v1', sequence: 1, entries: [entry()], security: [] }, mirror: '', networkError: false, requests: [], installed: [], confirm: async () => {} }
  const helper = { call: async (_cmd, [file]) => { const catalog = JSON.parse(fs.readFileSync(file)); if (catalog.invalid) throw new Error('Invalid catalog'); return { ok: true, catalog } } }
  const installer = { install: async (file, context) => { context.assertCurrent(); await state.confirm(); context.assertCurrent(); state.installed.push(context.identity); return { installed: true } } }
  const download = async (url, target, limit, signal) => {
    state.requests.push(url)
    if (state.networkError) throw new Error('offline')
    if (signal.aborted) throw new Error('cancelled')
    fs.writeFileSync(target, url === OFFICIAL_EXTENSION_INDEX ? JSON.stringify(state.index) : 'wheel')
  }
  const catalog = new ExtensionCatalog(path.join(root, 'catalog'), store, helper, installer, () => state.mirror, () => {}, download)
  return { root, store, catalog, state, reopen: () => new ExtensionCatalog(path.join(root, 'catalog'), store, helper, installer, () => state.mirror, () => {}, download) }
}
test('renderer may select identity/action only, never supply URL/digest/review/source', () => {
  for (const field of ['url', 'sha256', 'digest', 'review', 'source', 'mirror']) assert.throws(() => catalogRequest({ action: 'install', id: 'p', version: '1.0.0', [field]: 'forged' }))
  for (const version of ['1.0.0-beta.1', '1.0.0+build', '01.0.0', {}, null]) assert.throws(() => catalogRequest({ action: 'install', id: 'p', version }))
  assert.deepEqual(catalogRequest({ action: 'install', id: 'p', version: '1.0.0' }), { action: 'install', id: 'p', version: '1.0.0' })
})
test('official lookup and mirror bytes keep identity, empty entry is not installable', async t => {
  const f = fixture(t); f.state.mirror = 'https://mirror.example/files/'
  await f.catalog.install('org.example.demo', '0.1.0')
  assert.equal(f.state.requests[0], OFFICIAL_EXTENSION_INDEX)
  assert.equal(f.state.requests[1], 'https://mirror.example/files/' + 'a'.repeat(64) + '/demo-0.1.0-py3-none-any.whl')
  assert.equal(f.state.installed.length, 1)
  await assert.rejects(f.catalog.install('org.example.demo', '9.0.0'), /Select a version/)
})
test('cache deletion and offline refresh cannot clear persisted bans; future entries do not hide security', async t => {
  const f = fixture(t)
  f.state.index.entries[0].incompatible = ['future contract']
  f.state.index.security = [{ ...entry(), status: 'blocked', sequence: 1 }]
  await f.catalog.refresh()
  fs.unlinkSync(path.join(f.root, 'catalog/index.json'))
  f.state.networkError = true
  await assert.rejects(f.catalog.refresh(), /offline/)
  assert.match(f.store.extensionSecurityBlock({ id: entry().id, version: entry().version, digest: entry().sha256 }), /blocked/)
  assert.equal((await f.catalog.view()).entries.length, 1)
})
test('withdrawal, incompatibility, and changes during confirmation reject without commit', async t => {
  for (const reason of ['maintainer', 'copyright', 'future', 'confirmation']) {
    const f = fixture(t)
    if (reason === 'future') f.state.index.entries[0].incompatible = ['unsupported contract']
    else if (reason === 'confirmation') f.state.confirm = async () => { f.state.index.sequence++; f.state.index.entries[0].withdrawal = { reason: 'maintainer', evidence: 'https://example.org/' }; await f.catalog.refresh() }
    else f.state.index.entries[0].withdrawal = { reason, evidence: 'https://example.org/' }
    await assert.rejects(f.catalog.install(entry().id, entry().version))
    assert.equal(f.state.installed.length, 0)
    assert.deepEqual(fs.readdirSync(path.join(f.root, 'catalog/staging')), [])
    assert.equal(f.store.extensionSecurityBlock({ id: entry().id, version: entry().version, digest: entry().sha256 }), '')
  }
})
test('format/safety rejection and old sequence replay retain complete prior snapshot', async t => {
  const f = fixture(t)
  await f.catalog.refresh()
  f.state.index.sequence = 0
  await assert.rejects(f.catalog.refresh(), /rollback/)
  f.state.index.invalid = true
  await assert.rejects(f.catalog.refresh(), /Invalid catalog/)
  assert.equal(f.store.extensionSecurityState().sequence, 1)
  assert.equal((await f.catalog.view()).sequence, 1)
})
test('homepage goes through main HTTPS validation', async t => {
  const f = fixture(t)
  f.state.index.entries[0].display.homepage = 'javascript:alert(1)'
  await f.catalog.refresh()
  assert.throws(() => f.catalog.homepage(entry().id, entry().version), /HTTPS/)
})

test('a cached catalog displays offline but cannot authorize artifact installation', async t => {
  const f = fixture(t)
  await f.catalog.refresh()
  f.state.networkError = true
  const reopened = f.reopen()
  assert.equal((await reopened.view()).cached, true)
  await assert.rejects(reopened.install(entry().id, entry().version), /offline/)
  assert.equal(f.state.installed.length, 0)
})
test('cancel while confirmation is open prevents commit and removes staging', async t => {
  const f = fixture(t)
  f.state.confirm = async () => f.catalog.cancel()
  await assert.rejects(f.catalog.install(entry().id, entry().version), /cancelled/)
  assert.equal(f.state.installed.length, 0)
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'catalog/staging')), [])
})
