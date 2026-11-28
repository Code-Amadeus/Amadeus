import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { createSourceRequire, loadTypeScript } from './helpers/loadTypeScript.mjs'

const { memoryLookupStatusText } = loadTypeScript(new URL('../src/renderer/components/chatMemoryLookup.ts', import.meta.url))
const translations = JSON.parse(fs.readFileSync(new URL('../src/renderer/locales/zh-CN.json', import.meta.url), 'utf8'))
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const nodes = value => !value || typeof value !== 'object' ? [] : Array.isArray(value) ? value.flatMap(nodes)
  : [value, ...nodes(value.props?.children)]
const text = value => typeof value === 'string' ? value : Array.isArray(value) ? value.map(text).join('') : text(value?.props?.children || '')
const session = id => ({ id, title: `Synthetic ${id}`, character_id: 'synthetic-character' })
const selected = id => ({ ok: true, current_session_id: id, session: session(id), sessions: [session(id)], messages: [] })

// Drive the complete production ChatPage through its JSX handlers, subscriptions,
// effects and rerenders. Child surfaces and transport are inert synthetic fixtures.
function pageHarness(respond = () => undefined, options = {}) {
  const hooks = [], effects = [], requests = [], subscriptions = new Map()
  let cursor = 0, dirty = false, tree
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
  const react = {
    Fragment: Symbol.for('react.fragment'),
    useState(initial) {
      const index = cursor++
      hooks[index] ||= { value: typeof initial === 'function' ? initial() : initial }
      return [hooks[index].value, next => {
        const value = typeof next === 'function' ? next(hooks[index].value) : next
        if (!Object.is(value, hooks[index].value)) { hooks[index].value = value; dirty = true }
      }]
    },
    useRef(initial) { const index = cursor++; return hooks[index] ||= { current: initial } },
    useMemo(factory, deps) {
      const index = cursor++
      if (!hooks[index] || !same(hooks[index].deps, deps)) hooks[index] = { deps, value: factory() }
      return hooks[index].value
    },
    useCallback(callback, deps) { return react.useMemo(() => callback, deps) },
    useEffect(callback, deps) {
      const index = cursor++
      if (!hooks[index] || !same(hooks[index].deps, deps)) effects.push(() => {
        hooks[index]?.cleanup?.(); hooks[index] = { deps, cleanup: callback() }
      })
    },
  }
  const previousWindow = globalThis.window, previousStorage = globalThis.localStorage
  globalThis.window = { setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} }
  globalThis.localStorage = { getItem: () => null, setItem() {} }
  const url = new URL('../src/renderer/components/ChatPage.tsx', import.meta.url)
  const mocks = {
    react,
    '../../../../render/web/chat_image_attachment.mjs': { prepareImageAttachment: async () => ({ name: 'synthetic.png', mime: 'image/png', dataUrl: 'data:image/png;base64,c3ludGhldGlj', width: 1, height: 1, byteLength: 9 }) },
    './ChatBubble': { __esModule: true, default: 'chat-bubble' },
    './ChatSessionRail': { __esModule: true, default: 'session-rail' },
    './ChatWorkActivityCard': { __esModule: true, default: 'work-card' },
    './FluentIcon': { __esModule: true, default: 'icon' },
    './ProjectAppsPanel': { __esModule: true, default: 'project-apps' },
    './MemoryPanel': { __esModule: true, default: 'memory-panel' },
    './SettingsPrimitives': { SettingsButton: 'button' },
    './work/CrtWorkWidget': { __esModule: true, default: 'work-widget' },
    './desktopRuntimeSettings': options.runtimeSettings || {
      persistDesktopRuntimeSettings: async () => ({ persisted: true, pendingRevisions: {} }),
      markRuntimeSettingsApplied: async () => {},
    },
    '../i18n': { useI18n: () => ({ t: (source, vars = {}) => (options.locale === 'zh-CN' ? translations[source] || source : source)
      .replace(/\{([^}]+)\}/g, (_, name) => String(vars[name] ?? `{${name}}`)) }) },
    '../activeCharacter': { useActiveCharacter: () => null },
  }
  // import.meta only controls an unrelated development demo; no Vite runtime is needed.
  const source = fs.readFileSync(url, 'utf8').replace('import.meta.env.DEV', 'false')
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText
  const module = { exports: {} }
  new Function('require', 'module', 'exports', code)(createSourceRequire(url, mocks), module, module.exports)
  const props = { connected: true, renderActive: false, renderAssetUrl: '', renderRequested: false, onToggleRender() {},
    subscribe(method, callback) {
      if (!subscriptions.has(method)) subscriptions.set(method, new Set())
      subscriptions.get(method).add(callback)
      return () => subscriptions.get(method).delete(callback)
    },
    async send(method, params) {
      requests.push({ method, params })
      const response = respond(method, params)
      if (response !== undefined) return response
      if (method === 'session.list') return { ...selected('session-A'), current_character_id: 'synthetic-character' }
      if (method === 'session.load') return selected(params.session_id)
      if (method === 'session.open_context') return { ...selected('session-A'), sessions: [{ ...session('session-A'), context: { workItemId: 'synthetic-work' } }] }
      if (method === 'system.get_config') return { chat_supports_images: true, llm_provider: 'deepseek' }
      if (method === 'chat.send') return { status: 'ok', turn_id: params.turn_id }
      return {}
    }, ...options.props }
  const render = () => { cursor = 0; dirty = false; tree = module.exports.default(props); effects.splice(0).forEach(effect => effect()); return tree }
  const find = predicate => nodes(tree).find(predicate)
  render()
  const page = {
    requests, render,
    get input() { return find(node => node.type === 'textarea') },
    get lookup() { return find(node => node.props?.className?.split(' ').includes('chat-memory-lookup-button')) },
    get feedback() { return text(find(node => node.props?.id === 'chat-memory-lookup-feedback')) },
    get bubbles() { return nodes(tree).filter(node => node.type === 'chat-bubble').map(node => ({ role: node.props.role, text: node.props.text })) },
    get sends() { return requests.filter(request => request.method === 'chat.send') },
    get rail() { return find(node => node.type === 'session-rail') },
    find,
    type(value) { page.input.props.onChange({ target: { value } }); render() },
    lookupNow() { page.lookup.props.onClick(); render() },
    sendNow() { find(node => node.props?.className === 'chat-send-button').props.onClick(); render() },
    emit(method, payload) { for (const callback of [...subscriptions.get(method) || []]) callback(payload); render() },
    receipt(status, overrides = {}) { const { params } = page.sends.at(-1); page.emit('chat.memory_lookup', { session_id: params.session_id, turn_id: params.turn_id, status, ...overrides }) },
    setConnected(value) { props.connected = value; render() },
    async flush() { for (let count = 0; count < 12; count++) { await new Promise(resolve => setImmediate(resolve)); if (dirty) render() } },
    dispose() { hooks.forEach(hook => hook?.cleanup?.()); globalThis.window = previousWindow; globalThis.localStorage = previousStorage },
  }
  return page
}

async function ready(t, respond, options) { const page = pageHarness(respond, options); t.after(() => page.dispose()); await page.flush(); return page }
function finish(page) { const { params } = page.sends.at(-1); page.emit('chat.complete', { session_id: params.session_id, turn_id: params.turn_id, full_text: '' }) }

test('the composer exposes a labelled native one-shot button and an independent live status', async t => {
  const page = await ready(t)
  assert.equal(page.lookup.type, 'button', 'native buttons provide Tab, Enter and Space activation')
  assert.equal(page.lookup.props.type, 'button')
  assert.equal(page.lookup.props['aria-label'], 'Search saved memories')
  assert.equal(text(page.lookup), '')
  assert.match(page.lookup.props.title, /once.*text.*2000/)
  assert.equal(page.lookup.props['aria-describedby'], 'chat-memory-lookup-feedback')
  const status = page.find(node => node.props?.id === 'chat-memory-lookup-feedback')
  assert.equal(status.props.role, 'status')
  assert.equal(status.props['aria-live'], 'polite')
  assert.equal(page.feedback, '')
  assert.deepEqual(page.bubbles, [])
})

test('Chinese UI translates the button, validation and all backend status labels', async t => {
  const page = await ready(t, undefined, { locale: 'zh-CN' })
  assert.equal(page.lookup.props['aria-label'], '查已存记忆')
  page.lookupNow()
  assert.equal(page.feedback, translations['Enter specific text to search saved memories.'])
  for (const label of Object.values(memoryLookupStatusText)) assert.ok(translations[label], label)
})

test('the application toggle shares desktop settings and follows Host configuration', async t => {
  const saved = []
  const applied = []
  const page = await ready(t, (method, params) => {
    if (method === 'system.get_config') return { auip_interaction_enabled: true, llm_provider: 'deepseek' }
    if (method === 'system.set_config') return { values: params.values }
  }, { runtimeSettings: {
    persistDesktopRuntimeSettings: async values => {
      saved.push(values)
      return { persisted: true, pendingRevisions: { AUIP_INTERACTION_ENABLED: 7 } }
    },
    markRuntimeSettingsApplied: async (values, revisions) => { applied.push({ values, revisions }) },
  } })
  const toggle = () => page.find(node => node.props?.['aria-label'] === 'Let Amadeus interact with applications')
  page.type('keep this draft')
  assert.equal(toggle().props['aria-pressed'], true)
  assert.equal(text(toggle()), '')
  toggle().props.onClick()
  await page.flush()
  assert.equal(toggle().props['aria-pressed'], false)
  assert.deepEqual(saved, [{ auip_interaction_enabled: false }])
  assert.deepEqual(applied, [{ values: { auip_interaction_enabled: false }, revisions: { AUIP_INTERACTION_ENABLED: 7 } }])
  assert.equal(page.input.props.value, 'keep this draft')
  assert.equal(page.sends.length, 0)
  page.emit('system.config', { values: { auip_interaction_enabled: true } })
  assert.equal(toggle().props['aria-pressed'], true)
})

for (const query of ['Synthetic favourite game', '合成：以前提到的咖啡', '合成：前に話した旅行🪷']) {
  test(`explicit lookup sends the confirmed text verbatim: ${query}`, async t => {
    const page = await ready(t)
    page.type(query); page.lookupNow()
    assert.deepEqual(page.sends[0].params, { text: query, provider: 'deepseek', turn_id: page.sends[0].params.turn_id, session_id: 'session-A', memory_lookup: true })
    assert.ok(page.sends[0].params.turn_id)
    assert.equal(page.feedback, '', 'sending is not evidence that a search started')
    await page.flush()
    assert.equal(page.input.props.value, '')
    assert.deepEqual(page.bubbles, [], 'lookup status never manufactures chat messages')
  })
}

test('only the explicit action carries memory_lookup; Send, Enter and Shift+Enter retain ordinary behavior', async t => {
  const page = await ready(t)
  page.type('Search saved memories about synthetic tea')
  page.sendNow(); await page.flush()
  assert.equal(Object.hasOwn(page.sends[0].params, 'memory_lookup'), false)
  finish(page)
  page.type('以前の話を覚えてる？ synthetic')
  let prevented = false
  page.input.props.onKeyDown({ key: 'Enter', shiftKey: true, preventDefault() { prevented = true } })
  assert.equal(page.sends.length, 1)
  assert.equal(prevented, false)
  page.input.props.onKeyDown({ key: 'Enter', shiftKey: false, preventDefault() { prevented = true } })
  await page.flush()
  assert.equal(prevented, true)
  assert.equal(page.sends[1].params.text, '以前の話を覚えてる？ synthetic')
  assert.equal(Object.hasOwn(page.sends[1].params, 'memory_lookup'), false)
})

test('empty and oversized lookup drafts are refused without sending or losing the text', async t => {
  const page = await ready(t)
  for (const value of ['', '  \n\t', '🪷'.repeat(2001)]) {
    page.type(value); page.lookupNow(); await page.flush()
    assert.equal(page.sends.length, 0)
    assert.equal(page.input.props.value, value)
    assert.match(page.feedback, value.trim() ? /2000/ : /Enter specific text/)
    assert.deepEqual(page.bubbles, [])
  }
})

test('the 2000 character boundary counts Unicode codepoints as Python does', async t => {
  const page = await ready(t)
  const query = '🪷'.repeat(1999) + '日'
  page.type(query); page.lookupNow(); await page.flush()
  assert.equal(page.sends.length, 1)
  assert.equal(page.sends[0].params.text, query)
})

test('attached images stay attached and their draft remains intact when lookup is refused', async t => {
  const page = await ready(t)
  await page.find(node => node.type === 'input' && node.props.type === 'file').props.onChange({ target: { files: [{}], value: 'synthetic.png' } })
  page.render(); page.type('Synthetic image query'); page.lookupNow()
  assert.equal(page.sends.length, 0)
  assert.equal(page.input.props.value, 'Synthetic image query')
  assert.match(page.feedback, /text only.*Remove the image/)
  assert.ok(page.find(node => node.props?.['aria-label'] === 'Remove image'))
  page.sendNow(); await page.flush()
  assert.equal(page.sends.length, 1)
  assert.equal(page.sends[0].params.visual.mode, 'attachment')
  assert.equal(Object.hasOwn(page.sends[0].params, 'memory_lookup'), false)
})

test('artifact conversation context refuses lookup until the context is removed', async t => {
  const page = await ready(t)
  page.rail.props.onOpenDraftApps(); await page.flush()
  page.find(node => node.type === 'project-apps').props.onInteract({ workItemId: 'synthetic-work', artifactId: 'synthetic-artifact', title: 'Synthetic artifact' })
  await page.flush()
  page.type('Synthetic artifact query'); page.lookupNow()
  assert.equal(page.sends.length, 0)
  assert.equal(page.input.props.value, 'Synthetic artifact query')
  assert.match(page.feedback, /artifact context/)
})

test('offline, switching and busy lookup refusals retain the draft', async t => {
  const pending = deferred()
  const page = await ready(t, (method, params) => method === 'session.load' && params.session_id === 'session-B' ? pending.promise : undefined)
  page.type('Synthetic draft'); page.setConnected(false); page.lookupNow()
  assert.match(page.feedback, /Connect and open a chat/)
  assert.equal(page.input.props.value, 'Synthetic draft')
  page.setConnected(true); await page.flush()
  page.rail.props.onSelect('session-B'); page.lookupNow()
  assert.match(page.feedback, /chat switch/)
  assert.equal(page.sends.length, 0)
  pending.resolve(selected('session-B')); await page.flush()
  page.sendNow(); await page.flush()
  page.type('Synthetic next query'); page.lookupNow()
  assert.match(page.feedback, /current turn/)
  assert.equal(page.sends.length, 1)
  assert.equal(page.input.props.value, 'Synthetic next query')
})

test('a second click before React rerenders cannot submit a second lookup', async t => {
  const response = deferred()
  const page = await ready(t, method => method === 'chat.send' ? response.promise : undefined)
  page.type('Synthetic query')
  const click = page.lookup.props.onClick
  click(); click(); page.render()
  assert.equal(page.sends.length, 1)
  assert.equal(page.input.props.value, 'Synthetic query')
  response.resolve({ status: 'ok' }); await page.flush()
})

for (const status of Object.keys(memoryLookupStatusText)) {
  test(`only a real matching ${status} receipt displays its distinct status`, async t => {
    const page = await ready(t)
    page.type('Synthetic query'); page.lookupNow(); await page.flush()
    page.receipt(status)
    assert.equal(page.feedback, memoryLookupStatusText[status])
    assert.deepEqual(page.bubbles, [])
    assert.equal(page.requests.filter(request => /tts|translate|memory\./.test(request.method)).length, 0)
  })
}

test('role narration, provider results and chat completion cannot claim memory search execution', async t => {
  const page = await ready(t)
  page.type('Synthetic query'); page.lookupNow(); await page.flush()
  const { turn_id, session_id } = page.sends[0].params
  page.emit('chat.role_message', { turn_id, session_id, message_id: 'synthetic-role', text: 'Synthetic: I found all your memories.' })
  page.emit('provider.result', { metadata: { session_id, turn_id }, text: 'Synthetic: search complete' })
  finish(page)
  assert.equal(page.feedback, '')
  page.receipt('searching'); finish(page)
  assert.equal(page.feedback, memoryLookupStatusText.searching, 'chat.complete is not a search receipt')
})

test('foreign, malformed and out-of-order receipts cannot replace current execution facts', async t => {
  const page = await ready(t)
  page.type('Synthetic query'); page.lookupNow(); await page.flush()
  for (const overrides of [{ session_id: 'session-B' }, { session_id: '' }, { turn_id: 'foreign-turn' }, { turn_id: '' }, { status: 'invented' }]) page.receipt('found', overrides)
  assert.equal(page.feedback, '')
  page.receipt('found'); page.receipt('searching'); page.receipt('empty')
  assert.equal(page.feedback, memoryLookupStatusText.found)
  page.receipt('invalidated'); page.receipt('found')
  assert.equal(page.feedback, memoryLookupStatusText.invalidated)
})

test('a newer explicit request owns the status and late older receipts cannot displace it', async t => {
  const page = await ready(t)
  page.type('Synthetic first query'); page.lookupNow(); await page.flush()
  const first = page.sends[0].params
  page.receipt('found'); finish(page)
  page.type('Synthetic second query'); page.lookupNow(); await page.flush()
  assert.equal(page.sends.length, 2)
  assert.notEqual(page.sends[1].params.turn_id, first.turn_id)
  page.receipt('searching')
  page.emit('chat.memory_lookup', { session_id: first.session_id, turn_id: first.turn_id, status: 'empty' })
  assert.equal(page.feedback, memoryLookupStatusText.searching)
})

test('interruption, disconnect and a different confirmed user turn retire lookup receipts', async t => {
  const page = await ready(t)
  page.type('Synthetic query'); page.lookupNow(); await page.flush(); page.receipt('searching')
  const first = page.sends[0].params
  page.emit('chat.interrupted', { session_id: first.session_id, turn_id: first.turn_id, text: '' })
  page.receipt('found'); assert.equal(page.feedback, '')
  page.type('Synthetic next'); page.lookupNow(); await page.flush(); page.receipt('searching')
  page.emit('chat.user', { session_id: 'session-A', turn_id: 'synthetic-wallpaper-turn', text: 'Synthetic other surface input' })
  page.receipt('found'); assert.equal(page.feedback, '')
  page.emit('chat.complete', { session_id: 'session-A', turn_id: 'synthetic-wallpaper-turn', full_text: '' })
  page.type('Synthetic third'); page.lookupNow(); await page.flush(); page.receipt('searching')
  assert.equal(page.sends.length, 3)
  page.setConnected(false); page.receipt('found'); assert.equal(page.feedback, '')
})

test('switching clears lookup immediately and delayed responses cannot change the new chat or its draft', async t => {
  const response = deferred(), switchResponse = deferred()
  const page = await ready(t, (method, params) => method === 'chat.send' ? response.promise
    : method === 'session.load' && params.session_id === 'session-B' ? switchResponse.promise : undefined)
  page.type('Synthetic old draft'); page.lookupNow(); page.receipt('searching')
  page.rail.props.onSelect('session-B'); page.render()
  page.receipt('found'); assert.equal(page.feedback, '')
  switchResponse.resolve(selected('session-B')); await page.flush()
  page.type('Synthetic new session draft')
  response.reject(new Error('synthetic old error')); await page.flush()
  page.receipt('unavailable')
  assert.equal(page.feedback, '')
  assert.equal(page.input.props.value, 'Synthetic new session draft')
  assert.equal(page.rail.props.activeId, 'session-B')
  assert.deepEqual(page.bubbles, [])
})

for (const [label, respond, pattern] of [
  ['transport failure', () => Promise.reject(new Error('Synthetic private detail')), /request failed.*draft was kept/],
  ['explicit refusal', () => ({ ok: false, status: 'ok' }), /request failed.*draft was kept/],
  ['cancelled admission', () => ({ status: 'cancelled_before_admission' }), /request failed.*draft was kept/],
  ['replayed admission', () => ({ status: 'replayed' }), /no new memory search.*draft was kept/],
]) {
  test(`${label} preserves the draft, ends pending and never fabricates a result`, async t => {
    const page = await ready(t, method => method === 'chat.send' ? respond() : undefined)
    page.type('Synthetic query'); page.lookupNow(); await page.flush()
    assert.equal(page.input.props.value, 'Synthetic query')
    assert.match(page.feedback, pattern)
    page.receipt('found')
    assert.match(page.feedback, pattern)
    assert.deepEqual(page.bubbles, [])
    page.lookupNow(); await page.flush()
    assert.equal(page.sends.length, 2, 'refusal releases the pending turn')
  })
}

test('an asynchronous chat error is separate feedback and late admission cannot clear the draft', async t => {
  const response = deferred()
  const page = await ready(t, method => method === 'chat.send' ? response.promise : undefined)
  page.type('Synthetic draft'); page.lookupNow()
  const { session_id, turn_id } = page.sends[0].params
  page.emit('chat.error', { session_id, turn_id, error: 'Synthetic private detail' })
  assert.equal(page.feedback, 'This turn did not finish.')
  assert.deepEqual(page.bubbles, [])
  response.resolve({ status: 'ok' }); await page.flush()
  assert.equal(page.input.props.value, 'Synthetic draft')
  page.receipt('empty')
  assert.equal(page.feedback, 'This turn did not finish.')
})

test('typing or ASR during admission keeps the newer draft after the accepted lookup', async t => {
  const response = deferred()
  const page = await ready(t, method => method === 'chat.send' ? response.promise : undefined)
  page.type('Synthetic original'); page.lookupNow()
  page.emit('asr.recognized', { text: 'Synthetic new dictated text', is_final: true })
  const draft = page.input.props.value
  response.resolve({ status: 'ok' }); await page.flush()
  assert.equal(page.input.props.value, draft)
})

test('ordinary send can supersede a pending lookup without inheriting its flag or late failure', async t => {
  const response = deferred(); let sends = 0
  const page = await ready(t, method => method === 'chat.send' && ++sends === 1 ? response.promise : undefined)
  page.type('Synthetic lookup'); page.lookupNow(); page.receipt('searching')
  page.type('Synthetic ordinary follow-up'); page.sendNow(); await page.flush()
  assert.equal(Object.hasOwn(page.sends[1].params, 'memory_lookup'), false)
  response.reject(new Error('Synthetic delayed failure')); await page.flush()
  page.emit('chat.memory_lookup', { session_id: page.sends[0].params.session_id, turn_id: page.sends[0].params.turn_id, status: 'found' })
  assert.equal(page.feedback, '')
  assert.deepEqual(page.bubbles, [])
})

test('ordinary transport errors and session changes do not leave a permanent false busy turn', async t => {
  let fail = true
  const page = await ready(t, method => method === 'chat.send' && fail ? Promise.reject(Error('Synthetic ordinary failure')) : undefined)
  page.type('Synthetic ordinary'); page.sendNow(); await page.flush()
  fail = false
  page.type('Synthetic lookup'); page.lookupNow(); await page.flush()
  assert.equal(page.sends.length, 2)
  page.rail.props.onSelect('session-B'); await page.flush()
  page.type('Synthetic new session lookup'); page.lookupNow(); await page.flush()
  assert.equal(page.sends.length, 3)
  assert.equal(page.sends[2].params.session_id, 'session-B')
})

for (const status of ['found', 'empty', 'partial', 'unavailable', 'invalidated']) {
  test(`${status} remains an observed lookup fact when later reply generation fails`, async t => {
    const response = deferred()
    const page = await ready(t, method => method === 'chat.send' ? response.promise : undefined)
    page.type('Synthetic query'); page.lookupNow(); page.receipt(status)
    const { session_id, turn_id } = page.sends[0].params
    page.emit('chat.error', { session_id, turn_id, error: 'Synthetic role generation failure' })
    assert.equal(page.feedback, `${memoryLookupStatusText[status]} The reply could not be generated.`)
    assert.deepEqual(page.bubbles, [])
    response.resolve({ status: 'ok' }); await page.flush()
    assert.equal(page.input.props.value, 'Synthetic query', 'a later admission ack cannot erase the errored turn draft')
    assert.equal(page.feedback, `${memoryLookupStatusText[status]} The reply could not be generated.`)
    page.receipt(status)
    assert.equal(page.feedback, `${memoryLookupStatusText[status]} The reply could not be generated.`, 'duplicate receipts keep reply failure feedback')
  })
}

test('a found receipt after admission stays visible when the subsequent role reply fails', async t => {
  const page = await ready(t)
  page.type('Synthetic query'); page.lookupNow(); await page.flush()
  assert.equal(page.input.props.value, '')
  page.receipt('found')
  const { session_id, turn_id } = page.sends[0].params
  page.emit('chat.error', { session_id, turn_id, error: 'Synthetic role failure after retrieval' })
  assert.equal(page.feedback, `${memoryLookupStatusText.found} The reply could not be generated.`)
  assert.doesNotMatch(page.feedback, /draft|request failed/)
  assert.deepEqual(page.bubbles, [])
})

test('an errored searching turn ends neutrally without claiming an empty or unavailable result', async t => {
  const page = await ready(t)
  page.type('Synthetic query'); page.lookupNow(); await page.flush(); page.receipt('searching')
  const { session_id, turn_id } = page.sends[0].params
  page.emit('chat.error', { session_id, turn_id, error: 'Synthetic role failure' })
  assert.equal(page.feedback, 'This turn did not finish.')
  page.receipt('found')
  assert.equal(page.feedback, 'This turn did not finish.')
  assert.deepEqual(page.bubbles, [])
})

test('transport failure cannot erase an already observed lookup result', async t => {
  const response = deferred()
  const page = await ready(t, method => method === 'chat.send' ? response.promise : undefined)
  page.type('Synthetic query'); page.lookupNow(); page.receipt('found')
  response.reject(new Error('Synthetic disconnect')); await page.flush()
  assert.equal(page.feedback, `${memoryLookupStatusText.found} Reply completion could not be confirmed. Your draft was kept.`)
  assert.equal(page.input.props.value, 'Synthetic query')
})

test('a foreign or identity-free user event cannot retire an explicit lookup', async t => {
  const page = await ready(t)
  page.type('Synthetic query'); page.lookupNow(); await page.flush(); page.receipt('searching')
  for (const payload of [{ session_id: 'session-B', turn_id: 'synthetic-other' }, { turn_id: 'synthetic-other' }, { session_id: 'session-A' }]) {
    page.emit('chat.user', { text: 'Synthetic malformed or foreign', ...payload })
    assert.equal(page.feedback, memoryLookupStatusText.searching)
  }
  page.receipt('found')
  assert.equal(page.feedback, memoryLookupStatusText.found)
})

test('a genuine current user turn from another input surface blocks lookup through admission and streaming', async t => {
  const page = await ready(t)
  page.emit('chat.user', { session_id: 'session-A', turn_id: 'synthetic-passive', text: 'Synthetic voice input' })
  page.type('Synthetic draft'); page.lookupNow()
  assert.equal(page.sends.length, 0)
  assert.match(page.feedback, /current turn/)
  page.emit('chat.complete', { session_id: 'session-A', turn_id: 'synthetic-passive', full_text: '' })
  page.lookupNow(); await page.flush()
  assert.equal(page.sends.length, 1)
})

test('startup has explicit lookup feedback before Session restoration finishes', async t => {
  const sessions = deferred()
  const page = pageHarness(method => method === 'session.list' ? sessions.promise : undefined)
  t.after(() => page.dispose())
  page.type('Synthetic startup draft'); page.lookupNow()
  assert.equal(page.sends.length, 0)
  assert.match(page.feedback, /Connect and open a chat/)
  assert.equal(page.input.props.value, 'Synthetic startup draft')
  sessions.resolve({ ...selected('session-A'), current_character_id: 'synthetic-character' }); await page.flush()
})
