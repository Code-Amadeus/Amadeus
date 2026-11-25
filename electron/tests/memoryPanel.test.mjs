import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const state = loadTypeScript(new URL('../src/renderer/components/memoryPanelState.ts', import.meta.url))
const { MemoryPanelConnection, MemoryPanelFailure, memoryAttempt, selectedSourceSpan, sourceSpan, sourceTextParts,
  memoryErrorText, memoryIndexText, memorySemanticReasonText, memoryAutomaticAdmissionText,
  memorySourcePolicyReasonText, memorySourcePolicyActionText, memoryPolicyRecoveryCommand } = state
const context = { session_id: 'synthetic-session', character_id: 'synthetic-character', project_id: 'synthetic-project', context_token: 'synthetic-binding' }
const memory = { memory_id: 'synthetic-memory', row_version: 3, text: 'Synthetic preference', scope: { kind: 'character' }, state: 'active', index_state: 'pending' }
const ok = extra => ({ ok: true, context, ...extra })
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }

test('requests require a Host Session binding and cannot act or accept responses after disposal', async () => {
  const waiting = deferred(), requests = []
  const client = new MemoryPanelConnection(context.session_id, async (method, params) => {
    requests.push({ method, params })
    return method === 'memory.status' ? ok({ enabled: true }) : waiting.promise
  })
  await assert.rejects(client.request('memory.remember', { text: 'Synthetic' }), error => error.code === 'context_changed')
  assert.equal(requests.length, 0)
  await client.request('memory.status')
  const late = client.request('memory.list', { scope: 'project' })
  assert.deepEqual(requests[1].params, { scope: 'project', session_id: context.session_id, context_token: context.context_token })
  client.dispose()
  waiting.resolve(ok({ items: [memory] }))
  await assert.rejects(late, error => error.code === 'detached')
  await assert.rejects(client.request('memory.forget'), error => error.code === 'detached')
  assert.equal(requests.length, 2)
})

test('a foreign or changed successful context is rejected before content reaches the panel', async () => {
  const client = new MemoryPanelConnection(context.session_id, async method => method === 'memory.status'
    ? ok({}) : ok({ context: { ...context, context_token: 'changed-binding' }, items: [memory] }))
  await client.request('memory.status')
  await assert.rejects(client.request('memory.list'), error => error.code === 'context_changed')
  assert.equal(client.context, null)
})

test('retry identities and payload are allocated once, and source offsets count Unicode codepoints', () => {
  const source = { ...context, message_id: 'synthetic-message', revision: 2, projection_version: 'amadeus-memory-text-v1', role: 'user', text: 'A🪷日本語\r\nB' }
  const span = sourceSpan(source, 1, 4)
  assert.deepEqual(span, { session_id: context.session_id, message_id: source.message_id, revision: 2, projection_version: 'amadeus-memory-text-v1', start_cp: 1, end_cp: 4 })
  assert.deepEqual(sourceTextParts(source, 1, 4), ['A', '🪷日本', '語\r\nB'])
  assert.deepEqual(selectedSourceSpan(source, 1, 5), span, 'textarea UTF-16 offsets preserve the emoji codepoint')
  assert.equal(selectedSourceSpan(source, 2, 5), null, 'a split surrogate boundary is rejected')
  assert.deepEqual(selectedSourceSpan(source, 7, 8), { ...span, start_cp: 7, end_cp: 8 }, 'textarea CRLF normalization maps back to canonical coordinates')
  for (const [start, end] of [[-1, 2], [2, 2], [0, 100], [0.5, 2]]) assert.equal(sourceSpan(source, start, end), null)
  let ids = 0
  const params = { text: 'Synthetic correction', source: span, expected_version: 3, mode: 'error' }
  const attempt = memoryAttempt('memory.correct', params, () => `synthetic-${++ids}`)
  params.text = 'Edited later'; span.start_cp = 0
  assert.equal(attempt.params.text, 'Synthetic correction')
  assert.equal(attempt.params.source.start_cp, 1)
  assert.equal(attempt.params.expected_version, 3)
  assert.equal(attempt.params.mode, 'error')
  assert.equal(attempt.params.operation_id, 'synthetic-1')
  assert.equal(attempt.params.item_id, 'synthetic-2')
  assert.equal(ids, 2)
})

test('backend failures are friendly, stale edits actionable, and semantic coverage is explicit', () => {
  assert.match(memoryErrorText(new MemoryPanelFailure('conflict')), /latest version/)
  assert.match(memoryErrorText(new MemoryPanelFailure('context_changed')), /context changed/)
  assert.doesNotMatch(memoryErrorText(new Error('Traceback private-path synthetic-secret')), /Traceback|private-path|synthetic-secret/)
  assert.match(memoryIndexText('unavailable'), /lexical recall only/)
  assert.match(memoryIndexText('partial'), /incomplete/)
  assert.match(memorySemanticReasonText('warming'), /manage memories while it loads/)
  assert.match(memorySemanticReasonText('model_corrupt'), /failed file verification/)
  assert.match(memorySemanticReasonText('model_unsupported'), /model or runtime version is unsupported/)
  assert.equal(memorySemanticReasonText('Traceback private-path synthetic-secret'), '')
})

test('dynamic memory feedback and lifecycle labels have Chinese translations', () => {
  const translations = JSON.parse(fs.readFileSync(new URL('../src/renderer/locales/zh-CN.json', import.meta.url), 'utf8'))
  const labels = [
    ...['disabled', 'unavailable', 'stale_session', 'context_changed', 'conflict', 'not_found', 'invalid_request', 'busy', 'outcome_unknown', 'unknown'].map(code => memoryErrorText(new MemoryPanelFailure(code))),
    ...['forget_linked_scope_unavailable', 'forget_preview_stale', 'forget_confirmation_required'].map(reason => memoryErrorText(new MemoryPanelFailure('conflict', true, reason))),
    ...['ready', 'pending', 'warming', 'partial', 'failed', 'unavailable', 'disabled', 'unknown'].map(memoryIndexText),
    ...['warming', 'model_missing', 'model_corrupt', 'model_unsupported', 'dependencies_missing', 'index_pending', 'embedding_failed'].map(memorySemanticReasonText),
    ...['ready', 'gate_required', 'semantic_unavailable'].map(memoryAutomaticAdmissionText),
    'Memory source protections need recovery before chat history can be used.',
    'Memory source protections are active. Protected chat history remains usable.',
    'Automatic recall is experimental and may include unrelated memories.',
    'Semantic indexing finds candidates; it does not establish that a memory is relevant.',
    ...['starting', 'identity_mismatch', 'core_unreadable', 'core_missing', 'store_rollback', 'invalid_coordinates', 'sidecar_rebuild_required', 'publication_pending'].map(memorySourcePolicyReasonText),
    ...['starting', 'identity_mismatch', 'core_unreadable', 'invalid_coordinates', 'sidecar_rebuild_required', 'unknown'].map(memorySourcePolicyActionText),
    ...['active', 'superseded', 'retracted', 'expired', 'forgotten', 'unknown'].map(state.memoryLifecycleText),
  ]
  for (const label of labels) assert.ok(translations[label], label)
})

// Run the actual component's event handlers and effects without an Electron/DOM dependency.
function panelHarness(respond, props = {}) {
  const hooks = [], queued = [], requests = []
  let cursor = 0, dirty = false, tree
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
  const react = {
    useState(initial) {
      const index = cursor++
      if (!hooks[index]) hooks[index] = { value: typeof initial === 'function' ? initial() : initial }
      return [hooks[index].value, value => { hooks[index].value = typeof value === 'function' ? value(hooks[index].value) : value; dirty = true }]
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
      if (!hooks[index] || !same(hooks[index].deps, deps)) queued.push(() => {
        hooks[index]?.cleanup?.(); hooks[index] = { deps, cleanup: callback() }
      })
    },
  }
  const { default: MemoryPanel } = loadTypeScript(new URL('../src/renderer/components/MemoryPanel.tsx', import.meta.url), {
    react,
    './FluentIcon': { default: 'icon' },
    './SettingsPrimitives': { CardShell: 'card', SettingsButton: 'button', SettingsField: 'field', StatusPill: 'pill' },
    '../i18n': { useI18n: () => ({ t: (text, vars = {}) => text.replace(/\{([^}]+)\}/g, (_, name) => String(vars[name] ?? `{${name}}`)) }) },
    '../styles/memoryPanel.css': {},
  })
  const input = { sessionId: context.session_id, characterName: 'Synthetic character', visible: true, onClose() {},
    send: async (method, params) => { requests.push({ method, params }); return respond(method, params) }, ...props }
  const render = () => { cursor = 0; dirty = false; tree = MemoryPanel(input); queued.splice(0).forEach(effect => effect()); return tree }
  const nodes = value => !value || typeof value !== 'object' ? [] : Array.isArray(value) ? value.flatMap(nodes)
    : [value, ...nodes(value.props?.children)]
  const text = value => typeof value === 'string' ? value : Array.isArray(value) ? value.map(text).join('') : text(value?.props?.children || '')
  const find = predicate => nodes(tree).find(predicate)
  render()
  return { requests, render, get text() { return text(tree) },
    node: id => find(node => node.props?.id === id && node.props?.onChange),
    labelled: label => find(node => node.props?.['aria-label'] === label),
    button: label => find(node => node.type === 'button' && text(node) === label),
    async flush() { for (let count = 0; count < 12; count++) { await new Promise(resolve => setImmediate(resolve)); if (dirty) render() } },
    dispose() { hooks.forEach(hook => hook?.cleanup?.()) },
  }
}

function backend(method) {
  if (method === 'memory.status') return ok({ enabled: true, status: { state: 'starting', semantic_state: 'warming', semantic_reason: 'warming', cleanup_state: 'complete' },
    scopes: ['character', 'shared', 'project'].map(kind => ({ kind, available: true })) })
  if (method === 'memory.list') return ok({ items: [memory] })
  if (method === 'memory.sources') return ok({ sources: [] })
  if (method === 'memory.get') return ok({ memory, history: [{ ...memory, memory_id: 'synthetic-predecessor', state: 'retracted', text: 'Synthetic old value' }] })
  if (method === 'memory.forget_preview') return ok({ preview: { targets: [{ memory_id: memory.memory_id, row_version: 3 }], memories: [memory], source_exclusions: [] } })
  return ok({ memory, operation: { status: 'saved', memory_id: memory.memory_id, row_version: 3, index_state: 'pending' } })
}

test('manual save waits for the committed result and retries an uncertain outcome with identical identities', async () => {
  const commit = deferred(); let saves = 0
  const panel = panelHarness(method => method === 'memory.remember' ? ++saves === 1 ? commit.promise : backend(method) : backend(method))
  await panel.flush()
  assert.match(panel.text, /manage memories while it loads/)
  assert.equal(panel.button('New memory').props.disabled, false)
  panel.button('New memory').props.onClick(); panel.render()
  panel.node('memory-text').props.onChange({ target: { value: 'Synthetic explicit memory' } }); panel.render()
  panel.button('Save memory').props.onClick(); panel.render()
  assert.doesNotMatch(panel.text, /Memory saved durably\./)
  const first = panel.requests.find(request => request.method === 'memory.remember')
  assert.equal(first.params.source, undefined, 'manual entry must not attach the last message')
  assert.equal(first.params.user_id, undefined)
  assert.equal(first.params.project_id, undefined)
  commit.reject(new Error('Synthetic disconnected transport private-detail'))
  await panel.flush()
  assert.doesNotMatch(panel.text, /private-detail|Memory saved durably\./)
  assert.ok(panel.button('Retry same operation'))
  panel.button('Retry same operation').props.onClick(); await panel.flush()
  assert.deepEqual(panel.requests.filter(request => request.method === 'memory.remember')[1].params, first.params)
  assert.match(panel.text, /Memory saved durably\./)
  assert.match(panel.text, /Semantic indexing pending/)
  panel.dispose()
})

test('inspection labels history and correction sends expected version and error mode without expanding scope', async () => {
  const panel = panelHarness(backend)
  await panel.flush()
  panel.button('Synthetic preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  assert.match(panel.text, /Retracted as an error/)
  panel.button('Correct').props.onClick(); panel.render()
  panel.node('memory-correction-mode').props.onChange({ target: { value: 'error' } }); panel.render()
  panel.node('memory-text').props.onChange({ target: { value: 'Synthetic corrected value' } }); panel.render()
  panel.button('Save memory').props.onClick(); await panel.flush()
  const request = panel.requests.find(item => item.method === 'memory.correct')
  assert.equal(request.params.memory_id, memory.memory_id)
  assert.equal(request.params.expected_version, 3)
  assert.equal(request.params.mode, 'error')
  assert.equal(request.params.scope, undefined)
  panel.dispose()
})

test('forget confirms correction-family removal and sends only the selected item with expected version', async () => {
  const panel = panelHarness(method => method === 'memory.forget'
    ? ok({ operation: { status: 'forgotten', memory_id: memory.memory_id, cleanup_pending: true } }) : backend(method))
  await panel.flush()
  panel.button('Synthetic preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  panel.button('Forget').props.onClick(); await panel.flush()
  assert.match(panel.text, /all its correction history/)
  assert.match(panel.text, /paraphrases are not automatically found/)
  assert.equal(panel.requests.filter(item => item.method === 'memory.forget').length, 0)
  panel.button('Forget memory and history').props.onClick(); await panel.flush()
  const request = panel.requests.find(item => item.method === 'memory.forget')
  assert.equal(request.params.memory_id, memory.memory_id)
  assert.equal(request.params.expected_version, 3)
  assert.deepEqual(request.params.confirmed_targets, [{ memory_id: memory.memory_id, row_version: 3 }])
  assert.equal(request.params.text, undefined)
  assert.match(panel.text, /historical versions were forgotten/)
  panel.dispose()
})

test('retrying a completed forget confirms success and keeps the original operation identity', async () => {
  let attempts = 0
  const panel = panelHarness(method => method === 'memory.forget'
    ? ++attempts === 1 ? { ok: false, error: 'outcome_unknown' }
      : ok({ operation: { status: 'forgotten', memory_id: memory.memory_id, replayed: true } })
    : backend(method))
  await panel.flush()
  panel.button('Synthetic preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  panel.button('Forget').props.onClick(); await panel.flush()
  panel.button('Forget memory and history').props.onClick(); await panel.flush()
  assert.doesNotMatch(panel.text, /historical versions were forgotten/)
  panel.button('Retry same operation').props.onClick(); await panel.flush()
  const requests = panel.requests.filter(request => request.method === 'memory.forget')
  assert.deepEqual(requests[1].params, requests[0].params)
  assert.match(panel.text, /This memory and its historical versions were forgotten\./)
  assert.doesNotMatch(panel.text, /earlier operation was retired/)
  panel.dispose()
})

test('replaying a retired save does not claim that the forgotten memory was saved again', async () => {
  const panel = panelHarness(method => method === 'memory.remember'
    ? ok({ operation: { status: 'retired', memory_id: memory.memory_id, row_version: 1, replayed: true } })
    : backend(method))
  await panel.flush()
  panel.button('New memory').props.onClick(); panel.render()
  panel.node('memory-text').props.onChange({ target: { value: 'Synthetic retired save retry' } }); panel.render()
  panel.button('Save memory').props.onClick(); await panel.flush()
  assert.match(panel.text, /This earlier operation was retired\. It did not restore a forgotten memory\./)
  assert.doesNotMatch(panel.text, /Memory saved durably\.|historical versions were forgotten/)
  panel.dispose()
})

test('source selection uses the canonical selected span and the whole-message choice is explicit', async () => {
  const source = { session_id: context.session_id, message_id: 'synthetic-source', revision: 4,
    projection_version: 'amadeus-memory-text-v1', role: 'user', text: 'A🪷日本語' }
  const panel = panelHarness(method => method === 'memory.sources' ? ok({ sources: [source] }) : backend(method))
  await panel.flush()
  panel.button('New memory').props.onClick(); panel.render()
  panel.node('memory-text').props.onChange({ target: { value: 'Synthetic paraphrase of the selected source' } }); panel.render()
  panel.node('memory-source').props.onChange({ target: { value: '0' } }); panel.render()
  assert.equal(panel.button('Save memory').props.disabled, true, 'choosing a source does not silently attach the entire message')
  panel.labelled('Committed source message').props.onSelect({ currentTarget: { selectionStart: 1, selectionEnd: 5 } }); panel.render()
  panel.button('Use selected text').props.onClick(); panel.render()
  panel.button('Save memory').props.onClick(); await panel.flush()
  const request = panel.requests.find(item => item.method === 'memory.remember')
  assert.deepEqual(request.params.source, { session_id: context.session_id, message_id: source.message_id,
    revision: 4, projection_version: 'amadeus-memory-text-v1', start_cp: 1, end_cp: 4 })
  assert.equal(request.params.text, 'Synthetic paraphrase of the selected source')
  panel.dispose()
})

test('overlapping-source forget discloses and freezes every target; an unrelated fact remains outside the confirmed set', async () => {
  const linked = { ...memory, memory_id: 'synthetic-linked', row_version: 7, text: 'Synthetic overlapping fact' }
  const disjoint = { ...memory, memory_id: 'synthetic-disjoint', text: 'Synthetic unrelated fact' }
  const targets = [{ memory_id: memory.memory_id, row_version: 3 }, { memory_id: linked.memory_id, row_version: 7 }]
  let forgotten = false
  const panel = panelHarness(method => method === 'memory.forget_preview'
    ? ok({ preview: { targets, memories: [memory, linked], source_exclusions: [] } })
    : method === 'memory.forget' ? (forgotten = true, ok({ operation: { status: 'forgotten', memory_id: memory.memory_id } }))
      : method === 'memory.list' ? ok({ items: forgotten ? [disjoint] : [memory, linked, disjoint] }) : backend(method))
  await panel.flush()
  panel.button('Synthetic preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  panel.button('Forget').props.onClick(); await panel.flush()
  assert.match(panel.text, /Synthetic overlapping fact/)
  assert.match(panel.text, /every listed linked memory and its history/)
  assert.equal(panel.requests.filter(item => item.method === 'memory.forget').length, 0)
  panel.button('Forget all listed memories and history').props.onClick(); await panel.flush()
  const request = panel.requests.find(item => item.method === 'memory.forget')
  assert.deepEqual(request.params.confirmed_targets, targets)
  assert.ok(!request.params.confirmed_targets.some(item => item.memory_id === disjoint.memory_id))
  assert.match(panel.text, /Synthetic unrelated fact/)
  panel.dispose()
})

test('a disposed Session panel cannot apply late success or send another edit', async () => {
  const commit = deferred()
  const panel = panelHarness(method => method === 'memory.remember' ? commit.promise : backend(method))
  await panel.flush()
  panel.button('New memory').props.onClick(); panel.render()
  panel.node('memory-text').props.onChange({ target: { value: 'Synthetic old Session memory' } }); panel.render()
  panel.button('Save memory').props.onClick(); panel.render()
  panel.dispose()
  commit.resolve(backend('memory.remember')); await panel.flush()
  assert.doesNotMatch(panel.text, /Memory saved durably\./)
  assert.equal(panel.requests.filter(item => item.method === 'memory.remember').length, 1)
})

for (const [name, failure] of [
  ['an unknown rejection', { ok: false, error: 'synthetic_unknown_failure', message: 'private synthetic exception' }],
  ['a timed-out running mutation', { ok: false, error: 'outcome_unknown' }],
  ['a busy retry', { ok: false, error: 'busy' }],
  ['a successful response with changed context', ok({ context: { ...context, context_token: 'changed-binding' }, operation: { status: 'saved' } })],
]) test(`${name} retains the uncertain operation identity`, async () => {
  let first = true
  const panel = panelHarness(method => method === 'memory.remember' && first ? (first = false, failure) : backend(method))
  await panel.flush()
  panel.button('New memory').props.onClick(); panel.render()
  panel.node('memory-text').props.onChange({ target: { value: 'Synthetic uncertain outcome' } }); panel.render()
  panel.button('Save memory').props.onClick(); await panel.flush()
  assert.ok(panel.button('Retry same operation'))
  assert.doesNotMatch(panel.text, /Memory saved durably\.|private synthetic exception/)
  if (panel.button('Retry same operation').props.disabled) { panel.button('Refresh').props.onClick(); await panel.flush() }
  panel.button('Retry same operation').props.onClick(); await panel.flush()
  const saves = panel.requests.filter(item => item.method === 'memory.remember')
  assert.deepEqual(saves[1].params, saves[0].params)
  panel.dispose()
})

test('older memories remain reachable after the first 100 and duplicate page entries merge by identity', async () => {
  const firstPage = Array.from({ length: 100 }, (_, index) => ({ ...memory, memory_id: `synthetic-page-${index}`, text: `Synthetic page entry ${index}`, updated_at_ms: 1000 - index }))
  const oldest = { ...memory, memory_id: 'synthetic-oldest', text: 'Oldest retained preference', updated_at_ms: 1 }
  const cursor = [901, 'synthetic-page-99']
  const panel = panelHarness((method, params) => method === 'memory.list'
    ? ok(params.cursor ? { items: [firstPage[99], oldest], next_cursor: null } : { items: firstPage, next_cursor: cursor })
    : method === 'memory.get' ? ok({ memory: oldest, history: [] })
      : method === 'memory.forget_preview' ? ok({ preview: { targets: [{ memory_id: oldest.memory_id, row_version: 3 }], memories: [oldest], source_exclusions: [] } })
        : backend(method))
  await panel.flush()
  assert.doesNotMatch(panel.text, /Oldest retained preference/)
  panel.button('Load more memories').props.onClick(); await panel.flush()
  assert.match(panel.text, /Oldest retained preference/)
  assert.equal(panel.text.split('Synthetic page entry 99').length - 1, 1)
  assert.equal(panel.button('Load more memories'), undefined)
  const listRequest = panel.requests.filter(item => item.method === 'memory.list')[1]
  assert.deepEqual(listRequest.params.cursor, cursor)
  assert.equal(listRequest.params.scope, 'character')
  panel.button('Oldest retained preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  assert.ok(panel.button('Correct'))
  panel.button('Forget').props.onClick(); await panel.flush()
  const previewRequest = panel.requests.find(item => item.method === 'memory.forget_preview')
  assert.equal(previewRequest.params.memory_id, oldest.memory_id)
  assert.ok(panel.button('Forget memory and history'))
  panel.button('Cancel').props.onClick(); panel.render()
  panel.button('Refresh').props.onClick(); await panel.flush()
  assert.equal(panel.requests.filter(item => item.method === 'memory.list').at(-1).params.cursor, undefined)
  assert.doesNotMatch(panel.text, /Oldest retained preference/)
  panel.dispose()
})

test('switching scope discards a delayed older page instead of appending it to the new scope', async () => {
  const oldPage = deferred()
  const shared = { ...memory, memory_id: 'synthetic-shared', scope: { kind: 'shared' }, text: 'Synthetic shared memory' }
  const panel = panelHarness((method, params) => method === 'memory.list'
    ? params.cursor ? oldPage.promise : params.scope === 'shared' ? ok({ items: [shared], next_cursor: null })
      : ok({ items: [memory], next_cursor: [10, memory.memory_id] }) : backend(method))
  await panel.flush()
  panel.button('Load more memories').props.onClick(); panel.render()
  assert.equal(panel.node('memory-scope').props.disabled, false)
  panel.node('memory-scope').props.onChange({ target: { value: 'shared' } }); panel.render(); await panel.flush()
  assert.match(panel.text, /Synthetic shared memory/)
  oldPage.resolve(ok({ items: [{ ...memory, memory_id: 'stale-old-page', text: 'Stale character page must not appear' }], next_cursor: [1, 'stale-old-page'] }))
  await panel.flush()
  assert.doesNotMatch(panel.text, /Stale character page must not appear/)
  assert.equal(panel.button('Load more memories'), undefined)
  assert.match(panel.text, /Synthetic shared memory/)
  panel.dispose()
})

test('cleanup status unknown is shown without claiming completion or pending cleanup', async () => {
  const panel = panelHarness(method => method === 'memory.status'
    ? ok({ ...backend(method), status: { semantic_state: 'ready', cleanup_state: 'unknown' } }) : backend(method))
  await panel.flush()
  assert.match(panel.text, /Storage cleanup status unknown/)
  assert.doesNotMatch(panel.text, /Storage cleanup pending/)
  panel.dispose()
})

test('semantic readiness does not claim automatic admission when the model needs a gate', async () => {
  const panel = panelHarness(method => method === 'memory.status'
    ? ok({ ...backend(method), status: { semantic_state: 'ready', automatic_admission: 'gate_required' } }) : backend(method))
  await panel.flush()
  assert.match(panel.text, /Semantic index ready/)
  assert.match(panel.text, /Automatic use of memories in chat is awaiting validation\./)
  assert.doesNotMatch(panel.text, /Automatic recall ready/)
  assert.equal(panel.button('New memory').props.disabled, false)
  panel.dispose()
})

test('automatic recall readiness discloses experimental relevance without claiming a validated check', async () => {
  const panel = panelHarness(method => method === 'memory.status'
    ? ok({ ...backend(method), status: { semantic_state: 'ready', automatic_admission: 'ready' } }) : backend(method))
  await panel.flush()
  assert.match(panel.text, /Automatic recall ready/)
  assert.match(panel.text, /Automatic recall is experimental and may include unrelated memories\./)
  assert.match(panel.text, /Semantic indexing finds candidates; it does not establish that a memory is relevant\./)
  assert.doesNotMatch(panel.text, /relevance check ready|validated|calibrated/)
  panel.dispose()
})

test('source protection recovery stays visible when optional memory is disabled', async () => {
  const panel = panelHarness(() => ok({ enabled: false,
    status: { state: 'disabled', source_policy_state: 'recovery_required', source_policy_reason: 'sidecar_rebuild_required' } }))
  await panel.flush()
  assert.match(panel.text, /Memory source protections need recovery before chat history can be used\./)
  assert.match(panel.text, /derived memory source-protection file needs rebuilding/)
  assert.match(panel.text, /Stop the backend/)
  assert.ok(panel.text.includes(memoryPolicyRecoveryCommand))
  assert.equal(panel.button('New memory'), undefined)
  assert.equal(panel.requests.length, 1)
  panel.dispose()
})

for (const reason of ['identity_mismatch', 'core_missing', 'core_unreadable', 'store_rollback']) {
  test(`${reason} explains recovery without offering a policy rebuild or bypass`, async () => {
    const panel = panelHarness(() => ok({ enabled: false,
      status: { source_policy_state: 'recovery_required', source_policy_reason: reason } }))
    await panel.flush()
    assert.ok(panel.text.includes(memorySourcePolicyReasonText(reason)))
    assert.ok(panel.text.includes(memorySourcePolicyActionText(reason)))
    assert.ok(!panel.text.includes(memoryPolicyRecoveryCommand))
    assert.equal(panel.requests.length, 1)
    assert.equal(panel.button('Recover'), undefined)
    panel.dispose()
  })
}

test('verified source protections with pending publication do not claim that chat is blocked', async () => {
  const panel = panelHarness(() => ok({ enabled: false,
    status: { source_policy_state: 'ready', source_policy_reason: 'publication_pending' } }))
  await panel.flush()
  assert.match(panel.text, /Protected chat history remains usable/)
  assert.match(panel.text, /update has not finished/)
  assert.doesNotMatch(panel.text, /before chat history can be used/)
  assert.ok(!panel.text.includes(memoryPolicyRecoveryCommand))
  panel.dispose()
})

test('invalid coordinates do not promise that a sidecar rebuild repairs authoritative ranges', async () => {
  const panel = panelHarness(() => ok({ enabled: false,
    status: { source_policy_state: 'recovery_required', source_policy_reason: 'invalid_coordinates' } }))
  await panel.flush()
  assert.match(panel.text, /Invalid authoritative ranges cannot be repaired by rebuilding/)
  assert.match(panel.text, /restore matching verified database and policy backups/)
  assert.ok(panel.text.includes(memoryPolicyRecoveryCommand))
  panel.dispose()
})

test('unknown source protection reasons never display raw backend details', async () => {
  const panel = panelHarness(() => ok({ enabled: false,
    status: { source_policy_state: 'recovery_required', source_policy_reason: 'private-path synthetic-secret' } }))
  await panel.flush()
  assert.doesNotMatch(panel.text, /private-path|synthetic-secret/)
  assert.match(panel.text, /inspect its memory configuration/)
  panel.dispose()
})

test('a stale linked preview requires explicit review again and never silently expands forget targets', async () => {
  const panel = panelHarness(method => method === 'memory.forget'
    ? { ok: false, error: 'conflict', reason: 'forget_preview_stale' } : backend(method))
  await panel.flush()
  panel.button('Synthetic preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  panel.button('Forget').props.onClick(); await panel.flush()
  panel.button('Forget memory and history').props.onClick(); await panel.flush()
  assert.match(panel.text, /review the forget list again/)
  assert.equal(panel.button('Forget memory and history'), undefined)
  assert.equal(panel.requests.filter(item => item.method === 'memory.forget_preview').length, 1)
  assert.equal(panel.requests.filter(item => item.method === 'memory.forget').length, 1)
  panel.dispose()
})

test('an incomplete destructive preview cannot be confirmed', async () => {
  const panel = panelHarness(method => method === 'memory.forget_preview'
    ? ok({ preview: { targets: [{ memory_id: memory.memory_id, row_version: 3 }, { memory_id: 'unshown-linked', row_version: 4 }], memories: [memory] } }) : backend(method))
  await panel.flush()
  panel.button('Synthetic preferenceCurrent · Semantic indexing pending').props.onClick(); await panel.flush()
  panel.button('Forget').props.onClick(); await panel.flush()
  assert.equal(panel.button('Forget all listed memories and history'), undefined)
  assert.equal(panel.requests.filter(item => item.method === 'memory.forget').length, 0)
  assert.match(panel.text, /Could not confirm/)
  panel.dispose()
})

for (const [name, status] of [
  ['disabled', ok({ enabled: false, status: { state: 'disabled', semantic_state: 'disabled' } })],
  ['unavailable', { ok: false, error: 'unavailable', message: 'private import error' }],
]) test(`${name} memory leaves the panel closable and exposes no mutation controls`, async () => {
  let closed = false
  const panel = panelHarness(() => status, { onClose: () => { closed = true } })
  await panel.flush()
  assert.equal(panel.button('New memory'), undefined)
  assert.doesNotMatch(panel.text, /private import error/)
  panel.labelled('Back to chat').props.onClick()
  assert.equal(closed, true)
  assert.equal(panel.requests.filter(item => item.method !== 'memory.status').length, 0)
  panel.dispose()
})
