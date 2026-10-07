import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { automaticSessionSelection, runSessionSelection } from '../src/renderer/components/chatMessageState.ts'

const require = createRequire(import.meta.url)

// Exercise the actual ChatPage callbacks, without a duplicated selection flow.
const source = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('ChatPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const declarations = []
function visit(node) {
  if (ts.isVariableDeclaration(node) && ['selectSession', 'loadSession', 'restoreOwnedSession', 'handleDeleteSession'].includes(node.name.getText(tree))) {
    declarations.push(`const ${node.getText(tree)};`)
  }
  ts.forEachChild(node, visit)
}
visit(tree)
assert.equal(declarations.length, 4)
const compiled = ts.transpileModule(declarations.join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

function harness(request) {
  const state = {
    session: 'current', messages: [{ role: 'assistant', text: 'Current conversation.' }],
    context: { projectId: 'current-project' }, projectView: 'current-project', notice: '',
    pending: false, hydrated: [], requests: [],
  }
  const context = vm.createContext({
    useCallback: fn => fn,
    runSessionSelection,
    automaticSessionSelection,
    activeSession: 'current',
    sessions: [],
    setSessions: value => { state.sessions = value },
    setProjects: () => {},
    sessionSelectionRef: { current: { pending: 0 } },
    setSessionSwitching: value => { state.pending = value },
    setSessionSelectionNotice: value => { state.notice = value },
    setProjectViewId: value => { state.projectView = value },
    send: (method, params) => { state.requests.push({ method, params }); return request(method, params) },
    applySessionPayload: payload => {
      if (payload.current_session_id) state.session = payload.current_session_id
      if (Array.isArray(payload.messages)) state.messages = payload.messages
      if (payload.session) state.context = payload.session.context
    },
    hydrateWorkActivities: async id => { state.hydrated.push(id) },
  })
  vm.runInContext(compiled, context)
  return { state, load: vm.runInContext('loadSession', context), restore: vm.runInContext('restoreOwnedSession', context),
    delete: vm.runInContext('handleDeleteSession', context) }
}

test('rejected role selection displays the backend restart reason and preserves chat and context', async () => {
  const error = "This chat belongs to Mira. Restart the backend with Mira to open it."
  const page = harness(async () => ({ ok: false, error, messages: ['must not apply'], session: {} }))
  const before = structuredClone(page.state)
  await page.load('foreign-role', true)
  assert.equal(page.state.notice, error)
  assert.equal(page.state.session, before.session)
  assert.deepEqual(page.state.messages, before.messages)
  assert.deepEqual(page.state.context, before.context)
  assert.equal(page.state.projectView, before.projectView)
  assert.deepEqual(page.state.hydrated, [])
  assert.equal(page.state.pending, false)
})

test('transport failure has separate selection feedback and leaves the active conversation intact', async () => {
  const page = harness(async () => { throw new Error('Backend connection closed') })
  const before = structuredClone(page.state)
  await page.load('another-chat', true)
  assert.equal(page.state.notice, 'Backend connection closed')
  assert.equal(page.state.session, before.session)
  assert.deepEqual(page.state.messages, before.messages)
  assert.deepEqual(page.state.context, before.context)
  assert.equal(page.state.projectView, before.projectView)
  assert.equal(page.state.pending, false)
})

test('a successful selection applies its history and clears earlier selection feedback', async () => {
  let rejected = true
  const page = harness(async () => rejected ? { ok: false, detail: 'Please restart with Mira.' } : {
    ok: true, current_session_id: 'accepted', messages: [{ role: 'user', text: 'Accepted history' }],
    session: { context: { projectId: 'accepted-project' } },
  })
  await page.load('foreign-role')
  assert.equal(page.state.notice, 'Please restart with Mira.')
  rejected = false
  await page.load('accepted', true)
  assert.equal(page.state.notice, '')
  assert.equal(page.state.session, 'accepted')
  assert.deepEqual(page.state.messages, [{ role: 'user', text: 'Accepted history' }])
  assert.deepEqual(page.state.context, { projectId: 'accepted-project' })
  assert.equal(page.state.projectView, '')
  assert.deepEqual(page.state.hydrated, ['accepted'])
})

test('selection helper retains its rejection contract while reporting fallback feedback', async () => {
  const selection = { pending: 0 }
  let notice = ''
  await runSessionSelection(selection, () => {}, async () => ({ ok: false }),
    () => assert.fail('rejected selection must not apply'), value => { notice = value })
  assert.equal(notice, 'Could not switch chats.')
  const error = new Error('offline')
  await assert.rejects(runSessionSelection(selection, () => {}, async () => { throw error },
    () => assert.fail('transport failure must not apply'), value => { notice = value }), error)
  assert.equal(notice, 'offline')
  assert.equal(selection.pending, 0)
})

test('session row keeps its title and shows character ownership in its existing detail and tooltip', () => {
  const source = fs.readFileSync(new URL('../src/renderer/components/ChatSessionRail.tsx', import.meta.url), 'utf8')
  const tree = ts.createSourceFile('ChatSessionRail.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const row = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'SessionRow')
  const code = ts.transpileModule(row.getText(tree), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText
  const session = { id: 'foreign', title: 'Original chat title', character_id: 'mira', message_count: 2 }
  const context = vm.createContext({ require, exports: {}, useI18n: () => ({ t: key => key }), formatSessionTime: () => '',
    props: { session, active: false, onSelect() {}, onRename() {}, onDelete() {} } })
  vm.runInContext(code, context)
  const element = vm.runInContext('SessionRow(props)', context)
  const button = element.props.children[0]
  assert.equal(button.props.children[0].props.children, session.title)
  assert.equal(button.props.title, 'Original chat title\nCharacter: mira')
  const markup = require('react-dom/server').renderToStaticMarkup(element)
  assert.match(markup, />mira<\/span>/)
  assert.equal(session.title, 'Original chat title')
})

test('automatic recovery chooses its own role even when the latest or current candidate is foreign', async () => {
  const sessions = [
    { id: 'foreign', character_id: 'kurisu', timestamp: 100 },
    { id: 'older-own', character_id: 'mira', timestamp: 1 },
    { id: 'own', character_id: 'mira', timestamp: 2 },
  ]
  const page = harness(async (_method, params) => ({ ok: true, current_session_id: params.session_id,
    session: { context: {} }, messages: [] }))
  await page.restore(sessions, 'mira', 'foreign')
  assert.equal(page.state.session, 'own')
  assert.equal(page.state.projectView, 'current-project')
  assert.deepEqual(JSON.parse(JSON.stringify(page.state.requests)), [{ method: 'session.load', params: { session_id: 'own' } }])
  assert.equal(automaticSessionSelection(sessions, 'mira', 'older-own').id, 'older-own')
})

test('automatic recovery and deletion recovery create a current-role chat when only foreign history remains', async () => {
  const page = harness(async () => ({ ok: true, current_session_id: 'new-mira',
    session: { context: {} }, messages: [] }))
  await page.restore([{ id: 'foreign', character_id: 'kurisu', timestamp: 100 }], 'mira')
  assert.equal(page.state.session, 'new-mira')
  assert.deepEqual(JSON.parse(JSON.stringify(page.state.requests)), [{ method: 'session.create', params: {} }])
  assert.equal(automaticSessionSelection([], 'mira'), undefined)
})

test('automatic selection requires the same-version backend startup identity fact', () => {
  assert.throws(() => automaticSessionSelection([{ id: 'legacy', timestamp: 10 }], undefined),
    /Chat character identity is unavailable/)
  assert.equal(automaticSessionSelection([{ id: 'missing-owner', timestamp: 10 }], 'mira'), undefined)
})

for (const ownedHistory of [false, true]) {
  test(`deleting the active chat ${ownedHistory ? 'loads remaining same-role history' : 'creates a new same-role chat'}`, async () => {
    const sessions = [{ id: 'foreign', character_id: 'kurisu', timestamp: 100 },
      ...(ownedHistory ? [{ id: 'own', character_id: 'mira', timestamp: 1 }] : [])]
    const page = harness(async (method, params) => method === 'session.delete' ? {
      ok: true, current_character_id: 'mira', current_session_id: '', sessions,
    } : { ok: true, current_session_id: params.session_id || 'new-mira',
      session: { context: {} }, messages: [] })
    await page.delete('current')
    assert.equal(page.state.session, ownedHistory ? 'own' : 'new-mira')
    assert.deepEqual(page.state.requests.map(item => item.method), ['session.delete',
      ownedHistory ? 'session.load' : 'session.create'])
    if (ownedHistory) assert.equal(page.state.requests[1].params.session_id, 'own')
    assert.deepEqual(page.state.sessions, sessions) // Manual list retains foreign chats.
    assert.equal(page.state.notice, '')
    assert.equal(page.state.projectView, 'current-project')
  })
}
