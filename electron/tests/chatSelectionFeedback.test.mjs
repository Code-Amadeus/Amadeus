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
  if (ts.isVariableDeclaration(node) && ['toMessages', 'refreshSessions', 'applySessionPayload', 'selectSession', 'loadSession', 'restoreOwnedSession', 'handleDeleteSession'].includes(node.name.getText(tree))) {
    declarations.push(`const ${node.getText(tree)};`)
  }
  ts.forEachChild(node, visit)
}
visit(tree)
assert.equal(declarations.length, 7)
const compiled = ts.transpileModule(declarations.join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

function harness(request) {
  const state = {
    session: 'current', messages: [{ role: 'assistant', text: 'Current conversation.' }],
    sessions: [{ id: 'current', character_id: 'mira', context: { projectId: 'current-project' } }],
    projectView: 'current-project', notice: '', currentCharacterId: '',
    streaming: true, streamingText: 'In progress', activities: ['current-work'],
    translations: { current: 'translation' }, attention: ['current-attention'],
    attentionResolving: 'approve', attentionError: 'Current attention error', artifactContext: { workItemId: 'current-work' },
    pending: false, hydrated: [], requests: [],
  }
  Object.defineProperty(state, 'context', { enumerable: true,
    get: () => state.sessions.find(session => session.id === state.session)?.context || null })
  const refs = {
    activeSessionRef: { current: 'current' }, activeStreamTurnIdRef: { current: 'current-turn' },
    streamingTextRef: { current: 'In progress' }, lastAssistantTurnIdRef: { current: 'last-turn' },
    interruptedTurnIdsRef: { current: new Set(['interrupted-turn']) },
    chatTranslationRequestedRef: { current: new Set(['current']) }, chatTranslationGenerationRef: { current: 1 },
  }
  const context = vm.createContext({
    Error,
    useCallback: fn => fn,
    runSessionSelection,
    automaticSessionSelection,
    activeSession: 'current',
    sessions: state.sessions,
    ...refs,
    setSessions: value => { state.sessions = structuredClone(value) },
    setProjects: () => {},
    setCurrentCharacterId: value => { state.currentCharacterId = value },
    setActiveSession: value => { state.session = value },
    setMessages: value => { state.messages = structuredClone(value) },
    setWorkActivities: value => { state.activities = structuredClone(value) },
    setChatTranslations: value => { state.translations = structuredClone(value) },
    setStreaming: value => { state.streaming = value },
    setStreamingText: value => { state.streamingText = value },
    setAttentionRequests: value => { state.attention = structuredClone(value) },
    setAttentionResolving: value => { state.attentionResolving = value },
    setAttentionError: value => { state.attentionError = value },
    setArtifactContext: value => { state.artifactContext = structuredClone(value) },
    sessionSelectionRef: { current: { pending: 0 } },
    setSessionSwitching: value => { state.pending = value },
    setSessionSelectionNotice: value => { state.notice = value },
    setProjectViewId: value => { state.projectView = value },
    send: (method, params) => { state.requests.push({ method, params }); return request(method, params) },
    hydrateWorkActivities: async id => { state.hydrated.push(id) },
  })
  vm.runInContext(compiled, context)
  return { state, refs, refresh: vm.runInContext('refreshSessions', context),
    load: vm.runInContext('loadSession', context), restore: vm.runInContext('restoreOwnedSession', context),
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
    sessions: [{ id: 'accepted', character_id: 'mira', context: { projectId: 'accepted-project' } }],
  })
  await page.load('foreign-role')
  assert.equal(page.state.notice, 'Please restart with Mira.')
  rejected = false
  await page.load('accepted', true)
  assert.equal(page.state.notice, '')
  assert.equal(page.state.session, 'accepted')
  assert.deepEqual(page.state.messages, [{ role: 'user', text: 'Accepted history', turnId: undefined,
    messageId: undefined, streaming: false }])
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

test('the session rail labels only roles different from the backend startup role', () => {
  const source = fs.readFileSync(new URL('../src/renderer/components/ChatSessionRail.tsx', import.meta.url), 'utf8')
  const code = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText
  const sessions = [
    { id: 'own', title: 'Original own title', character_id: 'mira', message_count: 2 },
    { id: 'foreign', title: 'Original foreign title', character_id: 'kurisu', message_count: 2 },
  ]
  let stateIndex = 0
  const context = vm.createContext({ exports: {}, require: name => {
    if (name === 'react') return { useMemo: fn => fn(), useEffect() {}, useRef: value => ({ current: value }),
      useState: value => [stateIndex++ === 0 ? true : value, () => {}] }
    if (name === './FluentIcon') return { default: () => null }
    if (name === '../i18n') return { useI18n: () => ({ t: key => key }) }
    return require(name)
  }, props: { sessions, projects: [], activeId: 'foreign', currentCharacterId: 'mira', artifactViewId: '',
    onSelect() {}, onRename() {}, onDelete() {} } })
  vm.runInContext(code, context)
  const markup = require('react-dom/server').renderToStaticMarkup(vm.runInContext('exports.default(props)', context))
  const own = markup.match(/data-chat-session-id="own"[\s\S]*?<\/button>/)[0]
  const foreign = markup.match(/data-chat-session-id="foreign"[\s\S]*?<\/button>/)[0]
  assert.match(own, /title="Original own title"/)
  assert.doesNotMatch(own, /Character:|>mira<\/span>/)
  assert.match(foreign, /title="Original foreign title\nCharacter: kurisu"/)
  assert.match(foreign, />kurisu<\/span>/)
  assert.deepEqual(sessions.map(session => [session.title, session.character_id]),
    [['Original own title', 'mira'], ['Original foreign title', 'kurisu']])
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

test('startup creates a current-role chat when only foreign history remains', async () => {
  const page = harness(async () => ({ ok: true, current_session_id: 'new-mira',
    session: { context: {} }, messages: [] }))
  await page.restore([{ id: 'foreign', character_id: 'kurisu', timestamp: 100 }], 'mira')
  assert.equal(page.state.session, 'new-mira')
  assert.deepEqual(JSON.parse(JSON.stringify(page.state.requests)), [{ method: 'session.create', params: {} }])
  assert.equal(automaticSessionSelection([], 'mira'), undefined)
})

test('the startup role fact comes from session.list even when its current candidate is foreign', async () => {
  const sessions = [{ id: 'foreign', character_id: 'kurisu', timestamp: 100 },
    { id: 'own', character_id: 'mira', timestamp: 1 }]
  const page = harness(async (method, params) => method === 'session.list' ? {
    current_character_id: 'mira', current_session_id: 'foreign', sessions,
  } : { ok: true, current_session_id: params.session_id, messages: [], sessions })
  const { list, currentCharacterId, currentSessionId } = await page.refresh()
  await page.restore(list, currentCharacterId, currentSessionId)
  assert.equal(page.state.currentCharacterId, 'mira')
  assert.equal(page.state.session, 'own')
  assert.deepEqual(page.state.requests.map(item => item.method), ['session.list', 'session.load'])
})

test('automatic selection requires the same-version backend startup identity fact', () => {
  assert.throws(() => automaticSessionSelection([{ id: 'legacy', timestamp: 10 }], undefined),
    /Chat character identity is unavailable/)
  assert.equal(automaticSessionSelection([{ id: 'missing-owner', timestamp: 10 }], 'mira'), undefined)
})

for (const ownedHistory of [false, true]) {
  test(`deleting the active chat ${ownedHistory ? 'loads remaining same-role history' : 'leaves an empty chat while foreign history remains'}`, async () => {
    const sessions = [{ id: 'foreign', character_id: 'kurisu', timestamp: 100 },
      ...(ownedHistory ? [{ id: 'own', character_id: 'mira', timestamp: 1 }] : [])]
    const page = harness(async (method, params) => method === 'session.delete' ? {
      ok: true, current_character_id: 'mira', current_session_id: null, sessions,
    } : { ok: true, current_session_id: params.session_id || 'new-mira',
      session: { context: {} }, messages: [], sessions })
    await page.delete('current')
    assert.equal(page.state.session, ownedHistory ? 'own' : null)
    assert.equal(page.refs.activeSessionRef.current, ownedHistory ? 'own' : '')
    assert.deepEqual(page.state.requests.map(item => item.method), ownedHistory
      ? ['session.delete', 'session.load'] : ['session.delete'])
    if (ownedHistory) assert.equal(page.state.requests[1].params.session_id, 'own')
    assert.deepEqual(page.state.sessions, sessions) // Manual list retains foreign chats.
    assert.equal(page.state.notice, '')
    assert.equal(page.state.projectView, 'current-project')
    assert.deepEqual(page.state.messages, [])
    assert.deepEqual(page.state.activities, [])
    assert.deepEqual(page.state.translations, {})
    assert.deepEqual(page.state.attention, [])
    assert.equal(page.state.artifactContext, null)
    assert.equal(page.state.attentionResolving, '')
    assert.equal(page.state.attentionError, '')
    assert.equal(page.state.streaming, false)
    assert.equal(page.state.streamingText, '')
    assert.equal(page.refs.streamingTextRef.current, '')
    assert.equal(page.refs.activeStreamTurnIdRef.current, '')
    assert.equal(page.refs.lastAssistantTurnIdRef.current, '')
    assert.equal(page.refs.interruptedTurnIdsRef.current.size, 0)
    assert.equal(page.refs.chatTranslationRequestedRef.current.size, 0)
  })
}

for (const transportFailure of [false, true]) {
  test(`failed deletion ${transportFailure ? 'transport' : 'response'} preserves the active history, context, and artifact view`, async () => {
    const error = 'Could not delete this chat.'
    const page = harness(async () => {
      if (transportFailure) throw new Error(error)
      return { ok: false, error, current_session_id: null, sessions: [], messages: [] }
    })
    const before = structuredClone(page.state)
    await page.delete('current')
    assert.equal(page.state.notice, error)
    for (const key of ['session', 'sessions', 'messages', 'context', 'projectView', 'activities',
      'translations', 'attention', 'artifactContext', 'streaming', 'streamingText']) {
      assert.deepEqual(page.state[key], before[key], key)
    }
    assert.equal(page.refs.activeSessionRef.current, 'current')
    assert.deepEqual(page.state.hydrated, [])
    assert.equal(page.state.pending, false)
  })
}
