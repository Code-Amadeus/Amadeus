import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { automaticSessionSelection, runSessionSelection } from '../src/renderer/components/chatMessageState.ts'
import { foreignSessionCharacter, sessionCharacterNotice } from '../src/renderer/components/chatSessionCharacter.ts'

const require = createRequire(import.meta.url)

// Exercise the actual ChatPage callbacks, without a duplicated selection flow.
const source = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('ChatPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const declarations = []
let catalogEffectSource
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
    && node.arguments[0]?.getText(tree).includes('setSessionCharacters')) {
    catalogEffectSource = node.arguments[0].getText(tree)
  }
  if (ts.isVariableDeclaration(node) && ['toMessages', 'refreshSessions', 'applySessionPayload', 'selectSession', 'loadSession', 'restoreOwnedSession', 'handleDeleteSession', 'handleRenameSession'].includes(node.name.getText(tree))) {
    declarations.push(`const ${node.getText(tree)};`)
  }
  ts.forEachChild(node, visit)
}
visit(tree)
assert.equal(declarations.length, 8)
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
    pending: false, hydrated: [], requests: [], prompts: [], projects: [],
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
    setProjects: value => { state.projects = structuredClone(value) },
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
    window: { prompt: (...args) => { state.prompts.push(args); return 'Renamed chat' } },
  })
  vm.runInContext(compiled, context)
  return { state, refs, refresh: vm.runInContext('refreshSessions', context),
    load: vm.runInContext('loadSession', context), restore: vm.runInContext('restoreOwnedSession', context),
    delete: vm.runInContext('handleDeleteSession', context),
    rename: vm.runInContext('handleRenameSession', context) }
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

for (const [name, request] of [
  ['response without a reason', async () => ({ ok: false })],
  ['transport with an empty error', async () => { throw new Error('') }],
  ['transport with a non-Error rejection', async () => { throw { offline: true } }],
]) {
  test(`failed selection ${name} retains its switching fallback`, async () => {
    const page = harness(request)
    const before = structuredClone(page.state)
    await page.load('another-chat', true)
    assert.equal(page.state.notice, 'Could not switch chats.')
    assert.equal(page.state.session, before.session)
    assert.deepEqual(page.state.messages, before.messages)
    assert.deepEqual(page.state.context, before.context)
    assert.equal(page.state.projectView, before.projectView)
    assert.deepEqual(page.state.hydrated, [])
    assert.equal(page.state.pending, false)
  })
}

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

function compileModule(relative, dependencies = {}) {
  const code = ts.transpileModule(fs.readFileSync(new URL(relative, import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const exports = {}
  new Function('require', 'exports', code)(name => dependencies[name] || require(name), exports)
  return exports
}
const startup = compileModule('../src/main/backendStartup.ts')
const management = compileModule('../src/renderer/components/characterManagement.ts', { '../../main/backendStartup': startup })
assert.ok(catalogEffectSource)
const catalogEffectCode = ts.transpileModule(`const catalogEffect = ${catalogEffectSource}`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
const flush = () => new Promise(resolve => setImmediate(resolve))

function catalogHarness(send, getDesktopSettings) {
  const state = { characters: null, locked: false }
  const context = vm.createContext({
    connected: false, send, characterCatalog: management.characterCatalog,
    startupCharacterSelection: startup.startupCharacterSelection,
    window: getDesktopSettings ? { amadeus: { getDesktopSettings } } : {},
    setSessionCharacters: value => { state.characters = value },
    setStartupCharacterLocked: value => { state.locked = value },
  })
  vm.runInContext(catalogEffectCode, context)
  let cleanup
  return { state, render(connected) {
    cleanup?.(); context.connected = connected
    cleanup = vm.runInContext('catalogEffect()', context)
  } }
}

test('ChatPage reads fresh catalog names on connection and ignores results after disconnect or unmount', async () => {
  const catalogs = [], settings = []
  const page = catalogHarness(method => new Promise(resolve => { catalogs.push({ method, resolve }) }),
    () => new Promise(resolve => { settings.push(resolve) }))
  page.render(false)
  assert.equal(catalogs.length, 0)
  page.render(true)
  assert.equal(catalogs[0].method, 'character.list')
  page.render(false); page.render(true)
  catalogs[0].resolve({ characters: [role('other-role', 'Old name')] })
  settings[0]({ sources: { AMADEUS_CHARACTER_ID: 'environment' } })
  await flush()
  assert.equal(page.state.characters, null)
  assert.equal(page.state.locked, false)
  catalogs[1].resolve({ characters: [role('other-role', 'New name')] })
  settings[1]({ sources: { AMADEUS_CHARACTER_ID: 'user' } })
  await flush()
  assert.equal(page.state.characters[0].name, 'New name')
  assert.equal(page.state.locked, false)
  page.render(false)
  assert.equal(page.state.characters[0].name, 'New name', 'known offline labels are retained')
  const returnedPage = catalogHarness(async () => ({ characters: [role('other-role', 'Renamed in Settings')] }), async () => null)
  returnedPage.render(true); await flush()
  assert.equal(returnedPage.state.characters[0].name, 'Renamed in Settings')
})

test('catalog lookup failures stay unknown and only desktop locked/environment facts select environment guidance', async () => {
  const snapshots = [
    [null, false],
    [{ sources: { AMADEUS_CHARACTER_ID: 'user' } }, false],
    [{ sources: { AMADEUS_CHARACTER_ID: 'dotenv' } }, false],
    [{ sources: { AMADEUS_CHARACTER_ID: 'environment' } }, true],
    [{ locked: { AMADEUS_CHARACTER_ID: true } }, true],
  ]
  for (const [snapshot, expected] of snapshots) {
    const page = catalogHarness(async () => ({ characters: [] }), async () => snapshot)
    page.render(true); await flush()
    assert.equal(page.state.locked, expected)
    assert.deepEqual(page.state.characters, [])
  }
  for (const getSettings of [undefined, async () => { throw new Error('desktop unavailable') }]) {
    const page = catalogHarness(async () => { throw new Error('catalog unavailable') }, getSettings)
    page.render(true); await flush()
    assert.equal(page.state.characters, null)
    assert.equal(page.state.locked, false)
  }
})

const translate = (key, variables = {}) => key.replace(/\{(\w+)\}/g, (_, name) => variables[name] ?? `{${name}}`)
const railCode = ts.transpileModule(fs.readFileSync(new URL('../src/renderer/components/ChatSessionRail.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText

function rail(sessions, overrides = {}) {
  const calls = { select: [], rename: [], delete: [], notice: [] }
  let stateIndex = 0
  const context = vm.createContext({ exports: {}, require: name => {
    if (name === 'react') return { useMemo: fn => fn(), useEffect() {}, useRef: value => ({ current: value }),
      useState: value => [stateIndex++ === 0 ? true : value, () => {}] }
    if (name === './FluentIcon') return { default: () => null }
    if (name === '../i18n') return { useI18n: () => ({ t: translate }) }
    if (name === './chatSessionCharacter') return { foreignSessionCharacter, sessionCharacterNotice }
    return require(name)
  }, props: { sessions, projects: [], activeId: sessions[0]?.id, currentCharacterId: 'mira', artifactViewId: '',
    characters: [], connected: true, startupCharacterLocked: false,
    onSelect: id => calls.select.push(id),
    onRename: (id, title) => calls.rename.push([id, title]),
    onDelete: id => calls.delete.push(id), onNotice: notice => calls.notice.push(notice), ...overrides } })
  vm.runInContext(railCode, context)
  const element = vm.runInContext('exports.default(props)', context)
  const buttons = []
  const visit = node => {
    if (Array.isArray(node)) { node.forEach(visit); return }
    if (!node || typeof node !== 'object' || !node.props) return
    if (typeof node.type === 'function') { visit(node.type(node.props)); return }
    if (node.type === 'button') buttons.push(node)
    visit(node.props.children)
  }
  visit(element)
  return { calls, buttons, markup: require('react-dom/server').renderToStaticMarkup(element) }
}

const role = (character_id, name, valid = true) => ({ character_id, name, valid, persona: '', builtin: false, editable: true })
const rowMarkup = (markup, id) => markup.match(new RegExp(`data-chat-session-id="${id}"[\\s\\S]*?<\\/button>`))[0]

test('the session rail displays catalog names only for foreign roles and preserves original chat titles', () => {
  const sessions = [
    { id: 'own', title: 'Original own title', character_id: 'mira', message_count: 2 },
    { id: 'foreign', title: 'Original foreign title', character_id: 'kurisu', message_count: 2 },
  ]
  const { markup } = rail(sessions, { characters: [role('mira', 'Mira'), role('kurisu', 'Makise Kurisu')] })
  const own = rowMarkup(markup, 'own')
  const foreign = rowMarkup(markup, 'foreign')
  assert.match(own, /title="Original own title"/)
  assert.doesNotMatch(own, /Character:|>Mira<\/span>/)
  assert.match(foreign, /title="Original foreign title\nCharacter: Makise Kurisu \(kurisu\)/)
  assert.match(foreign, />Makise Kurisu<\/span>/)
  assert.match(foreign, /Settings → General → Character roles.*Use at next start.*restart/)
  assert.doesNotMatch(foreign, /AMADEUS_CHARACTER_ID/)
  assert.deepEqual(sessions.map(session => [session.title, session.character_id]),
    [['Original own title', 'mira'], ['Original foreign title', 'kurisu']])
})

test('same-named foreign roles retain full stable IDs in their tooltips and names fit the rail', () => {
  const ids = ['role-0123456789', 'role-9876543210']
  const name = '<Same & very long name>'
  const { markup } = rail(ids.map(id => ({ id, title: `Chat ${id}`, character_id: id })), {
    characters: ids.map(id => role(id, name)),
  })
  for (const id of ids) {
    const row = rowMarkup(markup, id)
    assert.ok(row.includes(`Character: &lt;Same &amp; very long name&gt; (${id})`))
    assert.match(row, /class="truncate" style="max-width:90px"/)
    assert.match(row, />&lt;Same &amp; very long name&gt;<\/span>/)
  }
})

test('missing and invalid roles are explicitly unavailable and never inherit the current role name', () => {
  const { markup } = rail([
    { id: 'missing', title: 'Missing role chat', character_id: 'removed-pack' },
    { id: 'invalid', title: 'Invalid role chat', character_id: 'broken-pack' },
  ], { characters: [role('mira', 'Current Mira'), role('broken-pack', 'Broken role', false)] })
  const missing = rowMarkup(markup, 'missing')
  const invalid = rowMarkup(markup, 'invalid')
  assert.match(missing, />removed-pack · Unavailable role<\/span>/)
  assert.match(invalid, />Broken role · Unavailable role<\/span>/)
  assert.match(invalid, /Character: Broken role · Unavailable role \(broken-pack\)/)
  for (const row of [missing, invalid]) {
    assert.match(row, /Restore or repair it before switching/)
    assert.doesNotMatch(row, /Current Mira/)
  }
})

test('an unreadable catalog uses a neutral stable ID rather than claiming a missing or invalid role', () => {
  const { markup, calls, buttons } = rail([{ id: 'foreign', title: 'Foreign chat', character_id: 'other-role' }], { characters: null })
  const row = rowMarkup(markup, 'foreign')
  assert.match(row, />other-role<\/span>/)
  assert.match(row, /title="Character: other-role"/)
  assert.doesNotMatch(row, /Unavailable role|Restore or repair|other-role \(other-role\)/)
  buttons.find(button => button.props['data-chat-session-id'] === 'foreign').props.onClick()
  assert.match(calls.notice[0], /Use at next start.*restart/)
  assert.doesNotMatch(calls.notice[0], /unavailable|Restore or repair/)
  assert.deepEqual(calls.select, [])
})

test('the actual rail callbacks deny foreign and unknown-owner access while same-role controls work', () => {
  const sessions = [
    { id: 'own', title: 'Own chat', character_id: 'mira' },
    { id: 'foreign', title: 'Foreign chat', character_id: 'kurisu' },
    { id: 'missing', title: 'Missing owner chat' },
    { id: 'invalid', title: 'Invalid owner chat', character_id: null },
  ]
  const { calls, buttons } = rail(sessions, { characters: [role('kurisu', 'Kurisu')] })
  for (const session of sessions) {
    const select = buttons.find(button => button.props['data-chat-session-id'] === session.id)
    const remove = buttons.find(button => button.props['aria-label'] === `Delete ${session.title}`)
    assert.ok(select, `listed title stays accessible for ${session.id}`)
    assert.ok(remove, `delete control remains labelled for ${session.id}`)
    assert.equal(select.props.children[0].props.children, session.title)
    assert.equal(Boolean(remove.props.disabled), session.id !== 'own')
    select.props.onDoubleClick()
    // Also exercise the handler behind native disabled-button behavior.
    remove.props.onClick({ stopPropagation() {} })
    select.props.onClick()
    if (session.id === 'foreign') {
      assert.match(select.props.title, /Character: Kurisu \(kurisu\)/)
      assert.match(remove.props.title, /Settings → General → Character roles.*Use at next start.*restart/)
    } else if (session.id !== 'own') {
      assert.match(remove.props.title, /identity is unavailable/)
    }
  }
  assert.deepEqual(calls.rename, [['own', 'Own chat']])
  assert.deepEqual(calls.delete, ['own'])
  assert.deepEqual(calls.select, ['own'])
  assert.equal(calls.notice.length, 9)
})

test('offline rail keeps known foreign names, omits current-role labels and denies all session actions', () => {
  const sessions = [
    { id: 'own', title: 'Own chat', character_id: 'mira' },
    { id: 'foreign', title: 'Foreign chat', character_id: 'kurisu' },
  ]
  const { markup, calls, buttons } = rail(sessions, {
    characters: [role('kurisu', 'Kurisu')], connected: false, startupCharacterLocked: true,
  })
  assert.doesNotMatch(rowMarkup(markup, 'own'), /Character:/)
  assert.match(rowMarkup(markup, 'foreign'), />Kurisu<\/span>/)
  for (const session of sessions) {
    const select = buttons.find(button => button.props['data-chat-session-id'] === session.id)
    const remove = buttons.find(button => button.props['aria-label'] === `Delete ${session.title}`)
    assert.equal(remove.props.disabled, true)
    select.props.onClick(); select.props.onDoubleClick(); remove.props.onClick({ stopPropagation() {} })
  }
  assert.deepEqual(calls.select, [])
  assert.deepEqual(calls.rename, [])
  assert.deepEqual(calls.delete, [])
  assert.equal(calls.notice.length, 6)
  for (const notice of calls.notice) {
    assert.match(notice, /Reconnect/)
    assert.doesNotMatch(notice, /Use at next start|AMADEUS_CHARACTER_ID/)
  }
})

test('only a known locked launch source displays environment instructions for a foreign chat', () => {
  const sessions = [{ id: 'foreign', title: 'Foreign chat', character_id: 'kurisu' }]
  for (const locked of [false, true]) {
    const { calls, buttons } = rail(sessions, { characters: [role('kurisu', 'Kurisu')], startupCharacterLocked: locked })
    buttons.find(button => button.props['data-chat-session-id'] === 'foreign').props.onClick()
    assert.equal(calls.select.length, 0)
    assert.equal(calls.notice.length, 1)
    if (locked) assert.match(calls.notice[0], /Change AMADEUS_CHARACTER_ID to kurisu.*launch environment.*restart/)
    else {
      assert.match(calls.notice[0], /Settings → General → Character roles.*Use at next start.*restart/)
      assert.doesNotMatch(calls.notice[0], /AMADEUS_CHARACTER_ID/)
    }
  }
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

const deletionFailures = [
  { name: 'response without a reason', response: { ok: false }, notice: 'Could not delete this chat.' },
  { name: 'response with empty reasons', response: { ok: false, message: '', detail: '  ', error: '' },
    notice: 'Could not delete this chat.' },
  { name: 'response with a backend detail', response: { ok: false, detail: 'This chat is still in use.' },
    notice: 'This chat is still in use.' },
  { name: 'response with a backend error', response: { ok: false, error: 'Deletion was refused by the backend.' },
    notice: 'Deletion was refused by the backend.' },
  { name: 'transport with an error message', error: new Error('Backend connection closed before deletion.'),
    notice: 'Backend connection closed before deletion.' },
  { name: 'transport with an empty error', error: new Error(''), notice: 'Could not delete this chat.' },
  { name: 'transport with a blank error', error: new Error('  '), notice: 'Could not delete this chat.' },
  { name: 'transport with a non-Error rejection', error: { offline: true }, notice: 'Could not delete this chat.' },
]

for (const failure of deletionFailures) {
  test(`failed deletion ${failure.name} reports its action and preserves the active history, context, and artifact view`, async () => {
    const page = harness(async () => {
      if ('error' in failure) throw failure.error
      return { ...failure.response, current_session_id: null, sessions: [], messages: [] }
    })
    const before = structuredClone(page.state)
    await page.delete('current')
    assert.equal(page.state.notice, failure.notice)
    for (const key of ['session', 'sessions', 'messages', 'context', 'projectView', 'activities',
      'translations', 'attention', 'artifactContext', 'streaming', 'streamingText']) {
      assert.deepEqual(page.state[key], before[key], key)
    }
    assert.equal(page.refs.activeSessionRef.current, 'current')
    assert.deepEqual(page.state.hydrated, [])
    assert.equal(page.state.pending, false)
  })
}

const renameFailures = [
  { response: { ok: false, error: 'Restart with AMADEUS_CHARACTER_ID=kurisu to manage this chat.' },
    notice: 'Restart with AMADEUS_CHARACTER_ID=kurisu to manage this chat.' },
  { response: { ok: false, message: '', detail: '  ', error: '' }, notice: 'Could not rename this chat.' },
  { response: { ok: false, detail: 'This chat is read-only.' }, notice: 'This chat is read-only.' },
  { error: new Error('Backend connection closed'), notice: 'Backend connection closed' },
  { error: new Error(' '), notice: 'Could not rename this chat.' },
  { error: { offline: true }, notice: 'Could not rename this chat.' },
]

for (const [index, failure] of renameFailures.entries()) {
  test(`failed rename ${index} reports standalone feedback without applying rejected payload or clearing active state`, async () => {
    const page = harness(async () => {
      if ('error' in failure) throw failure.error
      return { ...failure.response, current_session_id: null, sessions: [], projects: [], messages: [] }
    })
    const before = structuredClone(page.state)
    await page.rename('foreign', 'Foreign title')
    assert.equal(page.state.notice, failure.notice)
    for (const key of ['session', 'sessions', 'messages', 'context', 'projectView', 'activities', 'projects',
      'translations', 'attention', 'artifactContext', 'streaming', 'streamingText']) {
      assert.deepEqual(page.state[key], before[key], key)
    }
    assert.equal(page.refs.activeSessionRef.current, 'current')
    assert.deepEqual(page.state.requests.map(request => request.method), ['session.rename'])
    assert.equal(page.state.pending, false)
  })
}

test('successful rename applies updated titles and projects without replacing history or stopping a stream', async () => {
  const sessions = [{ id: 'current', character_id: 'mira', title: 'Renamed chat', context: { projectId: 'current-project' } }]
  const projects = [{ projectId: 'current-project', name: 'Project' }]
  const page = harness(async () => ({ ok: true, current_session_id: 'current', sessions, projects }))
  const before = structuredClone(page.state)
  page.state.notice = 'Earlier failure'
  await page.rename('current', 'Original title')
  assert.deepEqual(page.state.sessions, sessions)
  assert.deepEqual(page.state.projects, projects)
  assert.equal(page.state.notice, '')
  for (const key of ['session', 'messages', 'context', 'projectView', 'activities',
    'translations', 'attention', 'artifactContext', 'streaming', 'streamingText']) {
    assert.deepEqual(page.state[key], before[key], key)
  }
  assert.deepEqual(JSON.parse(JSON.stringify(page.state.requests)), [
    { method: 'session.rename', params: { session_id: 'current', title: 'Renamed chat' } },
  ])
})
