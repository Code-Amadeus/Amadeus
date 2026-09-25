import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { activityFromEvent, mergeActivity } from '../src/renderer/components/vnPresentation.ts'

// Exercise the page's actual async recovery callback, with state setters and
// transport substituted. The browser probe covers its React integration.
const source = readFileSync(new URL('../src/renderer/components/VNPage.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('VNPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const component = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'VNPage')
const refresh = component.body.statements.flatMap(node => ts.isVariableStatement(node) ? [...node.declarationList.declarations] : [])
  .find(node => node.name.getText(ast) === 'refresh')
const compiled = ts.transpileModule(`globalThis.refresh = ${refresh.initializer.getText(ast)}`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

const event = (session, seq) => ({ method: 'vn.line', payload: {
  session_id: session, event_id: `${session}:${seq}`, event_seq: seq,
  emitted_at_ms: seq, line: { text: `${session} line ${seq}` },
} })
const activity = (session, seq) => activityFromEvent('vn.line', event(session, seq).payload)
const snapshot = (session, historySession = session) => {
  const runtime = { status: 'active', profile: { session_id: session } }
  return {
    'vn.launch.profiles': { profiles: [{ id: session, name: session }] },
    'vn.launch.status': { status: 'active', sessionId: session, profileId: session, runtime },
    'vn.status': { ...runtime, profile: { session_id: historySession }, activity: [event(historySession, 1)] },
  }
}
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function harness(send) {
  const state = { launch: { sessionId: 'a' }, runtime: { profile: { session_id: 'a' } },
    activity: [activity('a', 1)], selectedProfile: 'a', error: '' }
  const sessionRef = { current: 'a' }
  const setters = Object.fromEntries(['profiles', 'launch', 'agentExe', 'overlayAvailable', 'activity',
    'capabilityPresets', 'selectedProfile', 'runtime', 'error'].map(key => [
    `set${key[0].toUpperCase()}${key.slice(1)}`,
    update => { state[key] = typeof update === 'function' ? update(state[key]) : update },
  ]))
  const context = vm.createContext({ connected: true, send, sessionRef, useCallback: fn => fn,
    activityFromEvent, mergeActivity, ...setters })
  vm.runInContext(compiled, context)
  return { state, sessionRef, refresh: context.refresh }
}

test('refresh adopts the returned session and replaces the previous game history', async () => {
  const response = snapshot('b')
  const page = harness(async method => response[method])
  await page.refresh()
  assert.equal(page.state.launch.sessionId, 'b')
  assert.equal(page.state.runtime.profile.session_id, 'b')
  assert.deepEqual(page.state.activity.map(item => item.id), ['b:1'])
})

test('late refresh cannot overwrite a session that changed while it was pending', async () => {
  const response = snapshot('a'), release = deferred()
  const page = harness(async method => { await release.promise; return response[method] })
  const pending = page.refresh()
  page.sessionRef.current = 'b'
  page.state.launch = { sessionId: 'b' }
  page.state.runtime = { profile: { session_id: 'b' } }
  page.state.activity = [activity('b', 1)]
  release.resolve()
  await pending
  assert.equal(page.state.launch.sessionId, 'b')
  assert.equal(page.state.runtime.profile.session_id, 'b')
  assert.deepEqual(page.state.activity.map(item => item.id), ['b:1'])
})

test('a late snapshot cannot overwrite a newer refresh that already adopted another session', async () => {
  const oldResponse = snapshot('a'), newResponse = snapshot('b'), release = deferred()
  let requests = 0
  const page = harness(async method => {
    if (++requests <= 3) { await release.promise; return oldResponse[method] }
    return newResponse[method]
  })
  const oldRefresh = page.refresh()
  await page.refresh()
  release.resolve()
  await oldRefresh
  assert.equal(page.state.launch.sessionId, 'b')
  assert.deepEqual(page.state.activity.map(item => item.id), ['b:1'])
})

for (const session of ['a', 'b']) {
  test(`recovery preserves live events while restoring history for session ${session}`, async () => {
    const response = snapshot(session), release = deferred()
    const page = harness(async method => { await release.promise; return response[method] })
    const pending = page.refresh()
    page.sessionRef.current = session
    page.state.activity = [activity(session, 2)]
    release.resolve()
    await pending
    assert.deepEqual(page.state.activity.map(item => item.id), [`${session}:1`, `${session}:2`])
  })
}

test('history from another runtime session is not displayed under the returned launch session', async () => {
  const response = snapshot('b', 'c')
  const page = harness(async method => response[method])
  await page.refresh()
  assert.equal(page.state.launch.sessionId, 'b')
  assert.deepEqual(page.state.activity, [])
})

test('history remains available after the launch session has stopped', async () => {
  const response = snapshot('a')
  response['vn.launch.status'].sessionId = ''
  response['vn.launch.status'].status = 'idle'
  const page = harness(async method => response[method])
  await page.refresh()
  assert.deepEqual(page.state.activity.map(item => item.id), ['a:1'])
})
