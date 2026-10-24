import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
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
  const installer = { pendingUpdate: () => state.pendingUpdate,
    rollbackPendingUpdate: async () => { state.rolledBack = true },
    install: async (file, context) => { context.assertCurrent(); await state.confirm(); context.assertCurrent(); state.installed.push(context.identity); return { installed: true } } }
  const download = async (url, target, limit, signal) => {
    state.requests.push(url)
    if (state.networkError) throw new Error('offline')
    if (signal.aborted) throw new Error('cancelled')
    fs.writeFileSync(target, url === OFFICIAL_EXTENSION_INDEX ? JSON.stringify(state.index) : 'wheel')
  }
  state.notifications = []
  const catalog = new ExtensionCatalog(path.join(root, 'catalog'), store, helper, installer, () => state.mirror, () => {}, download, updates => state.notifications.push(updates))
  return { root, store, helper, catalog, state, reopen: () => new ExtensionCatalog(path.join(root, 'catalog'), store, helper, installer, () => state.mirror, () => {}, download) }
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

test('production dispatch rejects forged links, exposes compact progress and reports native cancellation truthfully', async t => {
  const f = fixture(t); const opened = []
  assert.equal((await f.catalog.dispatch({ action: 'install', id: entry().id, version: entry().version, url: 'https://forged.test/' }, {}, async url => opened.push(url))).ok, false)
  await f.catalog.refresh()
  f.state.index.entries[0].display.repository = 'https://user:pass@example.org/'
  f.state.index.sequence++
  await f.catalog.refresh()
  assert.equal((await f.catalog.dispatch({ action: 'repository', id: entry().id, version: entry().version }, {}, async url => opened.push(url))).ok, false)
  assert.deepEqual(opened, [])
  f.state.confirm = async () => { assert.equal(f.catalog.status().stage, 'downloading'); f.catalog.cancel() }
  const result = await f.catalog.dispatch({ action: 'install', id: entry().id, version: entry().version }, {}, async () => {})
  assert.equal(result.cancelled, true)
  const status = await f.catalog.dispatch({ action: 'status' }, {}, async () => {})
  assert.deepEqual(Object.keys(status).sort(), ['ok', 'progress'])
  assert.equal(status.progress, undefined)
})

test('a preparation or persistence error is not misreported as cancellation', async t => {
  const f = fixture(t)
  f.state.confirm = async () => { f.catalog.cancel(); throw new Error('simulated persistence failure') }
  const result = await f.catalog.dispatch({action:'install',id:entry().id,version:entry().version},{},async()=>{})
  assert.equal(result.ok, false)
  assert.equal(result.cancelled, undefined)
  assert.match(result.error, /persistence failure/)
})


test('automatic checks default off, persist opt-in, notify only compatible updates, and never install', async t => {
  const f = fixture(t)
  const now = Date.now()
  await f.catalog.checkAutomatic(now)
  assert.deepEqual(f.state.requests, [])
  t.mock.method(f.store, 'storedExtensionRecords', () => [{id:entry().id,version:'0.0.1'}])
  assert.equal((await f.catalog.dispatch({action:'auto-check',enabled:true},{},async()=>{})).ok,true)
  f.state.index.entries.push({...entry(),version:'9.0.0',incompatible:['future']})
  await f.catalog.checkAutomatic(now)
  assert.deepEqual(f.state.notifications, [[{id:entry().id,version:'0.1.0'}]])
  assert.deepEqual(f.state.installed, [])
  await f.catalog.checkAutomatic(now+1000)
  assert.equal(f.state.requests.length,1)
  await f.catalog.checkAutomatic(now+86_400_001)
  assert.equal(f.state.notifications.length,1,'unchanged candidate does not repeat a notification')
  assert.equal((await f.reopen().view()).autoCheck,true)
  assert.throws(()=>catalogRequest({action:'auto-check',enabled:'true'}))
  assert.throws(()=>catalogRequest({action:'auto-check',enabled:true,url:'https://forged/'}))
})

test('scheduling pins the selected artifact without downloading or installing until a fresh startup check', async t => {
  const f = fixture(t)
  const scheduled = await f.catalog.dispatch({action:'schedule',id:entry().id,version:entry().version},{},async()=>{})
  assert.equal(scheduled.scheduled,true)
  assert.equal(scheduled.catalog.scheduledInstall.digest,entry().sha256)
  assert.deepEqual(f.state.installed,[])
  assert.deepEqual(f.state.requests,[OFFICIAL_EXTENSION_INDEX])
  await f.reopen().applyScheduled()
  assert.equal(f.state.installed.length,1)
  assert.deepEqual(f.state.requests,[OFFICIAL_EXTENSION_INDEX,OFFICIAL_EXTENSION_INDEX,entry().url])
  assert.equal((await f.catalog.view()).scheduledInstall,undefined)
})

test('offline or changed scheduled bytes cannot install, and failed intent is never automatically replayed', async t => {
  for(const failure of ['offline','digest','withdrawal']) {
    const f=fixture(t)
    await f.catalog.dispatch({action:'schedule',id:entry().id,version:entry().version},{},async()=>{})
    if(failure==='offline') f.state.networkError=true
    if(failure==='digest') f.state.index.entries[0].sha256='b'.repeat(64)
    if(failure==='withdrawal') f.state.index.entries[0].withdrawal={reason:'maintainer',evidence:'https://example.org/'}
    await assert.rejects(f.reopen().applyScheduled())
    assert.deepEqual(f.state.installed,[])
    assert.equal((await f.catalog.view()).scheduledInstall,undefined)
    const requests=f.state.requests.length
    await f.reopen().applyScheduled()
    assert.equal(f.state.requests.length,requests)
  }
})

test('rollback selection only queues recovery; no package changes occur in the running app', async t => {
  const f=fixture(t)
  f.state.pendingUpdate={id:entry().id,version:'0.2.0',previousVersion:'0.1.0',canConfirm:false,recoveryRequired:true}
  assert.equal((await f.catalog.dispatch({action:'rollback'},{},async()=>{})).scheduled,true)
  assert.equal(f.state.rolledBack,undefined)
  await f.reopen().applyScheduled()
  assert.equal(f.state.rolledBack,true)
  assert.equal((await f.catalog.view()).rollbackScheduled,false)
})


test('turning checks off during a response suppresses its eventual notification',async t=>{
  const f=fixture(t)
  t.mock.method(f.store,'storedExtensionRecords',()=>[{id:entry().id,version:'0.0.1'}])
  await f.catalog.dispatch({action:'auto-check',enabled:true},{},async()=>{})
  let release
  const gate=new Promise(resolve=>{release=resolve})
  t.mock.method(f.catalog,'refresh',async()=>{await gate;return {entries:[entry()]}})
  const pending=f.catalog.checkAutomatic(Date.now())
  await f.catalog.dispatch({action:'auto-check',enabled:false},{},async()=>{})
  release();await pending
  assert.deepEqual(f.state.notifications,[])
  assert.deepEqual(f.state.installed,[])
})


test('native local selection freezes exact prerelease bytes and installs only on the next start', async t => {
  const f = fixture(t)
  const wheel = path.join(f.root, 'demo-1.0.0rc1-py3-none-any.whl')
  fs.writeFileSync(wheel, 'selected-local-wheel')
  const digest = createHash('sha256').update(fs.readFileSync(wheel)).digest('hex')
  t.mock.method(f.helper, 'call', async (command, [file]) => {
    assert.equal(command, 'inspect-wheel')
    return { ok: true, manifest: { id: 'org.example.local', version: '1.0.0rc1' }, digest: createHash('sha256').update(fs.readFileSync(file)).digest('hex') }
  })
  await f.catalog.scheduleLocal(wheel)
  assert.deepEqual((await f.catalog.view()).scheduledInstall, {id:'org.example.local', version:'1.0.0rc1', digest})
  assert.deepEqual(f.state.installed, [])
  assert.deepEqual(f.state.requests, [], 'local import has no catalog lookup or runtime download')
  fs.writeFileSync(wheel, 'the original may change after selection')
  f.state.confirm = async () => { assert.equal(f.state.installed.length, 0) }
  await f.reopen().applyScheduled()
  assert.equal(f.state.installed.length, 1)
  assert.equal(f.state.installed[0].digest, digest)
  assert.equal(f.state.installed[0].version, '1.0.0rc1')
  assert.equal((await f.catalog.view()).scheduledInstall, undefined)
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'catalog/pending-local')), [])
})

test('changed local snapshot or native cancellation is not installed or replayed; queued local import can be cancelled', async t => {
  for (const failure of ['tamper', 'confirmation', 'cancel']) {
    const f = fixture(t)
    const wheel = path.join(f.root, 'demo-1.0.0-py3-none-any.whl')
    fs.writeFileSync(wheel, 'selected-local-wheel')
    t.mock.method(f.helper, 'call', async (_command, [file]) => ({ok:true,manifest:{id:'org.example.local',version:'1.0.0'},digest:createHash('sha256').update(fs.readFileSync(file)).digest('hex')}))
    await f.catalog.scheduleLocal(wheel)
    const preferences = JSON.parse(fs.readFileSync(path.join(f.root, 'catalog/update-preferences.json')))
    const snapshot = path.join(f.root, 'catalog/pending-local', preferences.local.directory, preferences.local.file)
    if (failure === 'cancel') {
      assert.equal((await f.catalog.dispatch({action:'cancel-scheduled'},{},async()=>{})).ok, true)
      assert.equal(fs.existsSync(snapshot), false)
    } else {
      if (failure === 'tamper') fs.writeFileSync(snapshot, 'different bytes')
      else f.state.confirm = async () => { throw new Error('Native confirmation cancelled') }
      await assert.rejects(f.reopen().applyScheduled(), failure === 'tamper' ? /bytes changed/ : /cancelled/)
    }
    assert.deepEqual(f.state.installed, [])
    assert.equal((await f.catalog.view()).scheduledInstall, undefined)
    await f.reopen().applyScheduled()
    assert.deepEqual(f.state.installed, [])
    assert.deepEqual(fs.readdirSync(path.join(f.root, 'catalog/pending-local')), [])
  }
})

test('rejected local inspection never replaces the prior user selection', async t => {
  const f = fixture(t)
  await f.catalog.dispatch({action:'schedule',id:entry().id,version:entry().version},{},async()=>{})
  const wheel = path.join(f.root, 'demo-1.0.0-py3-none-any.whl')
  fs.writeFileSync(wheel, 'invalid-wheel')
  t.mock.method(f.helper, 'call', async () => { throw new Error('Invalid wheel') })
  await assert.rejects(f.catalog.scheduleLocal(wheel), /Invalid wheel/)
  assert.deepEqual((await f.catalog.view()).scheduledInstall, {id:entry().id,version:entry().version,digest:entry().sha256})
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'catalog/pending-local')), [])
})
