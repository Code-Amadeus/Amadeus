import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import {
  inspectedVisualProfile, receiveVisualSnapshot, selectedVisualProfile,
  visualConfigurationChanged, visualStatusFromFrame,
} from '../src/renderer/components/characterVisuals.ts'

const require = createRequire(import.meta.url)
const profile = {
  profile_id: 'visual-artwork-1', kind: 'live2d', name: 'Artwork',
  model_path: 'C:\\models\\art.model3.json',
  emotion_map: { smile: 'Smile', shy: null },
  mouth: { gain: 1, smoothing_ms: 60, parameter_ids: [] },
  layouts: { render: { scale: 1, x: 0, y: 0 }, wallpaper: { scale: 1.2, x: 0.1, y: -0.2 } },
}
const snapshot = {
  config: { backend: 'sprite', selected_profile_id: profile.profile_id, core_path: '', profiles: [profile] },
  capabilities: { expressions: ['Smile', 'Thinking'], lip_sync_ids: ['PARAM_MOUTH_OPEN_Y'], warnings: [] },
  surfaces: {},
}
const runtimeDiagnostic = {
  expression: 'Smile', mouth_ids: ['PARAM_MOUTH_OPEN_Y'], warnings: ['Optional motion unavailable'],
  render_texture: { width: 640, height: 480, resolution: 1 },
}
const readyStatus = { runtime_id: 'host-instance-1', profile_id: profile.profile_id, revision: 7, state: 'ready', diagnostic: runtimeDiagnostic }

test('Host status observations preserve the unsaved visual draft and update applied facts', () => {
  const loaded = receiveVisualSnapshot({ applied: null, draft: null }, snapshot)
  const draft = structuredClone(loaded.draft)
  draft.backend = 'live2d'
  draft.profiles[0].name = 'Unsaved artwork'
  const observation = { ...snapshot, surfaces: { render: readyStatus } }
  const editor = receiveVisualSnapshot({ ...loaded, draft }, observation)
  assert.equal(editor.applied.surfaces.render.state, 'ready')
  assert.equal(editor.applied.config.backend, 'sprite')
  assert.equal(editor.draft.backend, 'live2d')
  assert.equal(selectedVisualProfile(editor.draft).name, 'Unsaved artwork')
  assert.equal(visualConfigurationChanged(editor), true)
  const accepted = receiveVisualSnapshot(editor, { ...observation, config: draft }, true)
  assert.equal(visualConfigurationChanged(accepted), false)
  assert.notEqual(accepted.draft, draft, 'draft edits cannot mutate the Host snapshot')
})

test('a Host selection update follows applied facts only when no local changes exist', () => {
  const initial = receiveVisualSnapshot({ applied: null, draft: null }, snapshot)
  const updated = { ...snapshot, config: { ...snapshot.config, backend: 'live2d' } }
  const editor = receiveVisualSnapshot(initial, updated)
  assert.equal(editor.draft.backend, 'live2d')
  assert.equal(visualConfigurationChanged(editor), false)
})

test('replacing a model retains visual identity and layouts while refreshing model mappings', () => {
  const replacement = { ...profile, profile_id: 'new-inspection-id', model_path: 'C:\\models\\new.model3.json',
    emotion_map: { smile: 'NewSmile', shy: null }, mouth: { gain: 1, smoothing_ms: 60, parameter_ids: [] } }
  const edited = inspectedVisualProfile(replacement, profile)
  assert.equal(edited.profile_id, profile.profile_id)
  assert.equal(edited.name, profile.name)
  assert.deepEqual(edited.layouts, profile.layouts)
  assert.deepEqual(edited.emotion_map, replacement.emotion_map)
  assert.ok(!('character_id' in edited))
  assert.equal(inspectedVisualProfile(replacement).profile_id, 'new-inspection-id')
  assert.equal(inspectedVisualProfile({ ...replacement, model_path: profile.model_path }, profile), profile,
    'reinspection of the same model preserves edited mappings')
})

test('status messages require the owned frame and preserve structured diagnostics', () => {
  const owned = {}, unrelated = {}
  const message = { type: 'amadeus.character.status', status: readyStatus }
  assert.equal(visualStatusFromFrame(unrelated, owned, message, message.type), null)
  assert.equal(visualStatusFromFrame(owned, null, message, message.type), null)
  assert.equal(visualStatusFromFrame(owned, owned, { ...message, type: 'amadeus.visual.preview.status' }, message.type), null)
  assert.equal(visualStatusFromFrame(owned, owned, message, message.type), readyStatus)
  assert.equal(visualStatusFromFrame(owned, owned, { ...message, status: { ...readyStatus, state: 'pretend-ready' } }, message.type), null)
  assert.equal(visualStatusFromFrame(owned, owned, { ...message, status: { ...readyStatus, revision: '7' } }, message.type), null)
  assert.equal(visualStatusFromFrame(owned, owned, { ...message, status: { ...readyStatus, runtime_id: '' } }, message.type), null)
})

const pageSource = fs.readFileSync(new URL('../src/renderer/components/CharacterVisualsPage.tsx', import.meta.url), 'utf8')
const pageTree = ts.createSourceFile('CharacterVisualsPage.tsx', pageSource, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX)
const declarationNames = new Set(['postPreview', 'closePreview', 'run', 'handlePreviewMessage', 'openPreview', 'resetPreview', 'save', 'reload'])
const declarations = []
function visit(node) {
  if (ts.isVariableDeclaration(node) && declarationNames.has(node.name.getText(pageTree))) declarations.push('const ' + node.getText(pageTree) + ';')
  ts.forEachChild(node, visit)
}
visit(pageTree)
assert.equal(declarations.length, declarationNames.size)
const helpers = pageTree.statements.filter(statement => ts.isFunctionDeclaration(statement)
  && ['previewFingerprint', 'previewContext', 'snapshotFromResponse'].includes(statement.name?.text))
  .map(statement => statement.getText(pageTree)).join('\n')
const callbackCode = ts.transpileModule(helpers + '\n' + declarations.join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function previewHarness(response = async () => ({ url: 'file:///preview.html', config: {
  runtime_id: 'host-instance-1', profile_id: profile.profile_id, revision: 7,
}, capabilities: snapshot.capabilities })) {
  const owned = {}
  const state = { preview: { url: 'file:///preview.html', config: { runtime_id: 'host-instance-1', profile_id: profile.profile_id, revision: 7 }, status: null },
    mouth: 0.7, busy: '', error: '', notice: '', messages: [], requests: [], accepted: [] }
  const previewRef = { current: state.preview }, requestRef = { current: 0 }
  const context = vm.createContext({
    useCallback: fn => fn, visualStatusFromFrame, Error,
    profile, draft: { ...snapshot.config, backend: 'live2d' }, previewSurface: 'wallpaper',
    t: key => key,
    previewRef, previewRequestRef: requestRef,
    previewFrameRef: { current: { contentWindow: { postMessage: message => state.messages.push(structuredClone(message)) } } },
    setPreview: value => { state.preview = value }, setMouthValue: value => { state.mouth = value },
    setBusy: value => { state.busy = value }, setError: value => { state.error = value },
    setNotice: value => { state.notice = value }, setCapabilities: () => {},
    acceptSnapshot: (value, replaceDraft = false) => state.accepted.push({ value, replaceDraft }),
    send: async (method, params) => { state.requests.push({ method, params: structuredClone(params) }); return response(method, params) },
  })
  // Use the actual postMessage target object as event.source.
  vm.runInContext(callbackCode, context)
  return { state, previewRef, requestRef, context,
    owned: vm.runInContext('previewFrameRef.current.contentWindow', context),
    call: (name, value) => vm.runInContext(name, context)(value),
  }
}
const settle = () => new Promise(resolve => setImmediate(resolve))

test('preview handshake waits for its own frame and status never enters the Host observation channel', () => {
  const page = previewHarness()
  page.call('handlePreviewMessage', { source: {}, data: { type: 'amadeus.visual.preview.ready' } })
  assert.deepEqual(page.state.messages, [])
  page.call('handlePreviewMessage', { source: page.owned, data: { type: 'amadeus.visual.preview.ready' } })
  assert.equal(page.state.messages[0].action, 'configure')
  page.call('handlePreviewMessage', { source: page.owned, data: { type: 'amadeus.visual.preview.status', status: readyStatus } })
  assert.equal(page.state.preview.status.state, 'ready')
  assert.deepEqual(page.state.preview.status.diagnostic, runtimeDiagnostic)
  page.call('handlePreviewMessage', { source: page.owned, data: { type: 'amadeus.visual.preview.status', status: { ...readyStatus, revision: 6 } } })
  assert.equal(page.state.preview.status.revision, 7)
  page.call('resetPreview')
  assert.equal(page.state.messages.at(-1).action, 'reset')
  assert.equal(page.state.mouth, 0)
  page.call('closePreview')
  assert.equal(page.state.messages.at(-1).action, 'destroy')
  assert.equal(page.previewRef.current, null)
  assert.equal(page.state.preview, null)
  assert.deepEqual(page.state.requests, [], 'preview tests and readiness cannot affect global speech or applied state')
})

test('opening a draft preview uses its unsaved profile and selected surface without saving', async () => {
  const page = previewHarness()
  page.call('openPreview')
  await settle()
  assert.deepEqual(page.state.requests.map(value => value.method), ['visual.preview'])
  assert.equal(page.state.requests[0].params.surface, 'wallpaper')
  assert.deepEqual(page.state.requests[0].params.profile, profile)
  assert.equal(page.state.preview.config.revision, 7)
  assert.equal(page.state.messages[0].action, 'destroy', 'replacement disposes the previous instance')
})

test('a preview request completed after closing cannot resurrect its frame', async () => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  const page = previewHarness(async () => pending)
  page.call('openPreview')
  page.call('closePreview')
  finish({ url: 'file:///preview.html', config: { runtime_id: 'host-instance-1', profile_id: profile.profile_id, revision: 7 }, capabilities: snapshot.capabilities })
  await settle()
  assert.equal(page.state.preview, null)
  assert.equal(page.previewRef.current, null)
})

test('save accepts Host confirmation while reload preserves the unsaved draft', async () => {
  const page = previewHarness(async () => snapshot)
  page.call('save')
  await settle()
  assert.equal(page.state.requests[0].method, 'visual.save')
  assert.equal(page.state.requests[0].params.config.backend, 'live2d')
  assert.equal(page.state.accepted[0].replaceDraft, true)
  page.call('reload')
  await settle()
  assert.equal(page.state.requests[1].method, 'visual.reload')
  assert.deepEqual(page.state.requests[1].params, {})
  assert.equal(page.state.accepted[1].replaceDraft, false)
})

test('RPC failures appear in action feedback without accepting a configuration', async () => {
  const page = previewHarness(async () => { throw new Error('Core file is missing') })
  page.call('save')
  await settle()
  assert.equal(page.state.error, 'Core file is missing')
  assert.equal(page.state.busy, '')
  assert.deepEqual(page.state.accepted, [])
})

test('real React rendering accepts structured ready and error diagnostics', () => {
  const code = ts.transpileModule(pageSource, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  const context = vm.createContext({ exports: {}, require: name => {
    if (name === '../i18n') return { useI18n: () => ({ t: key => key }) }
    if (name === './characterVisuals') return {}
    if (name.endsWith('.css')) return {}
    return require(name)
  } })
  vm.runInContext(code, context)
  const React = require('react'), { renderToStaticMarkup } = require('react-dom/server')
  for (const diagnostic of [runtimeDiagnostic, { ...runtimeDiagnostic, expression: '', render_texture: null }]) {
    const markup = renderToStaticMarkup(React.createElement(context.exports.VisualDiagnostic, { diagnostic }))
    assert.match(markup, /PARAM_MOUTH_OPEN_Y/)
    assert.match(markup, /Optional motion unavailable/)
    assert.match(markup, /<details/)
    assert.ok(!markup.includes('[object Object]'))
  }
})

test('ChatPage only forwards an owned render observation and fixes the surface', async () => {
  const source = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
  const tree = ts.createSourceFile('ChatPage.tsx', source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX)
  let declaration
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'handleCharacterStatus') declaration = node
    ts.forEachChild(node, visit)
  }
  visit(tree)
  const code = ts.transpileModule('const ' + declaration.getText(tree), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText
  const owned = {}, requests = [], lastStatus = { current: null }
  const context = vm.createContext({ useCallback: fn => fn, visualStatusFromFrame,
    renderFrameRef: { current: { contentWindow: owned } }, renderStatusRef: lastStatus,
    send: async (method, params) => { requests.push({ method, params }) }, console })
  vm.runInContext(code, context)
  const handler = vm.runInContext('handleCharacterStatus', context)
  handler({ source: {}, data: { type: 'amadeus.character.status', status: readyStatus } })
  handler({ source: owned, data: { type: 'amadeus.visual.preview.status', status: readyStatus } })
  assert.equal(requests.length, 0)
  handler({ source: owned, data: { type: 'amadeus.character.status', status: { ...readyStatus, surface: 'wallpaper' } } })
  assert.equal(requests[0].method, 'visual.status')
  assert.equal(requests[0].params.surface, 'render')
  assert.deepEqual(requests[0].params.diagnostic, runtimeDiagnostic)
  assert.equal(lastStatus.current.state, 'ready')
})

const mainSource = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const mainTree = ts.createSourceFile('index.ts', mainSource, ts.ScriptTarget.ES2022, true)
const pickerNode = mainTree.statements.find(statement => ts.isExpressionStatement(statement)
  && ts.isCallExpression(statement.expression)
  && statement.expression.arguments[0]?.text === 'visual-file.select')
const pickerCode = ts.transpileModule('const picker = ' + pickerNode.expression.arguments[1].getText(mainTree), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function pickerHarness({ trusted = true, primaryFrame = true, file = 'C:\\models\\art.model3.json', canceled = false } = {}) {
  const options = []
  const sender = { mainFrame: {} }
  const context = vm.createContext({ path: path.win32, mainWindow: {},
    isMainRenderer: () => trusted,
    dialog: { showOpenDialog: async (_window, value) => {
      options.push(value)
      return { canceled, filePaths: [file] }
    } } })
  vm.runInContext(pickerCode, context)
  const picker = vm.runInContext('picker', context)
  return { options, choose: kind => picker({ sender, senderFrame: primaryFrame ? sender.mainFrame : {} }, kind, file) }
}

test('visual picker accepts only the main controller frame and known purposes', async () => {
  for (const options of [{ trusted: false }, { primaryFrame: false }]) {
    const picker = pickerHarness(options)
    const result = await picker.choose('model')
    assert.equal(result.ok, false)
    assert.deepEqual(picker.options, [])
  }
  const picker = pickerHarness()
  assert.equal((await picker.choose('tts')).ok, false)
  assert.deepEqual(picker.options, [])
})

test('visual picker validates the model entry and exact local Core filename', async () => {
  const model = pickerHarness()
  assert.equal((await model.choose('model')).path, 'C:\\models\\art.model3.json')
  assert.deepEqual(Array.from(model.options[0].filters[0].extensions), ['json'])
  assert.equal((await pickerHarness({ file: 'C:\\models\\arbitrary.json' }).choose('model')).ok, false)
  assert.equal((await pickerHarness({ file: 'C:\\sdk\\live2dcubismcore.min.js' }).choose('core')).ok, true)
  assert.equal((await pickerHarness({ file: 'C:\\sdk\\other.js' }).choose('core')).ok, false)
  const canceled = await pickerHarness({ canceled: true }).choose('core')
  assert.equal(canceled.cancelled, true)
  assert.equal(canceled.path, '')
})

const chatSource = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
const chatTree = ts.createSourceFile('ChatPage.tsx', chatSource, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX)
const connectionCallbacks = [], connectionNames = new Set(['postRenderEvent', 'handleRenderFrameLoad', 'handleCharacterStatus'])
let connectionEffect
function findConnectionCode(node) {
  if (ts.isVariableDeclaration(node) && connectionNames.has(node.name.getText(chatTree))) connectionCallbacks.push('const ' + node.getText(chatTree) + ';')
  if (ts.isCallExpression(node) && node.expression.getText(chatTree) === 'useEffect'
    && node.arguments[0]?.getText(chatTree).includes('renderConnectionRef.current')) connectionEffect = node.arguments[0]
  ts.forEachChild(node, findConnectionCode)
}
findConnectionCode(chatTree)
assert.equal(connectionCallbacks.length, connectionNames.size)
assert.ok(connectionEffect)
const connectionCode = ts.transpileModule(connectionCallbacks.join('\n') + '\nconst restoreConnection = '
  + connectionEffect.getText(chatTree) + ';', { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
function connectionHarness({ connected = true, wasConnected = connected, active = true, mounted = true,
  url = 'file:///render.html', loadedUrl = '' } = {}) {
  const requests = [], messages = []
  const owned = { postMessage: message => messages.push(structuredClone(message)) }
  const context = vm.createContext({
    useCallback: callback => callback, visualStatusFromFrame, console, RENDER_BRIDGE_MESSAGE: 'amadeus.render.event',
    connected, renderActive: active, renderAssetUrl: url,
    renderFrameRef: { current: mounted ? { contentWindow: owned } : null },
    renderFrameLoadedUrlRef: { current: loadedUrl }, renderConnectionRef: { current: wasConnected },
    renderStatusRef: { current: null },
    send: async (method, params) => { requests.push({ method, params: structuredClone(params) }); return {} },
  })
  vm.runInContext(connectionCode, context)
  return { context, owned, requests, messages, call: (name, value) => vm.runInContext(name, context)(value) }
}

test('an already loaded render clears mouth on disconnect and replays once on reconnect', () => {
  const page = connectionHarness()
  page.call('restoreConnection')
  assert.deepEqual(page.requests, [])
  page.call('handleRenderFrameLoad')
  assert.deepEqual(page.requests.map(request => request.method), ['render.ready'])
  page.context.connected = false
  page.call('restoreConnection')
  assert.deepEqual(page.messages.map(message => [message.method, message.params]),
    [['render.speaking', { speaking: false }], ['render.mouth', { value: 0 }]])
  page.context.connected = true
  page.call('restoreConnection')
  page.call('handleCharacterStatus', { source: page.owned, data: { type: 'amadeus.character.status', status: readyStatus } })
  page.call('restoreConnection')
  assert.equal(page.requests.filter(request => request.method === 'render.ready').length, 2,
    'readiness observations and unchanged connected state cannot cause repeated replay')
})

test('render recovery waits for an owned loaded frame and handles loading while offline', () => {
  for (const options of [
    { active: false }, { mounted: false }, { url: '' }, { loadedUrl: '' }, { loadedUrl: 'file:///previous.html' },
  ]) {
    const page = connectionHarness({ wasConnected: false, ...options })
    page.call('restoreConnection')
    assert.deepEqual(page.requests, [])
  }
  const offline = connectionHarness({ connected: false })
  offline.call('handleRenderFrameLoad')
  assert.deepEqual(offline.requests, [])
  assert.equal(offline.context.renderFrameLoadedUrlRef.current, 'file:///render.html')
  offline.context.connected = true
  offline.call('restoreConnection')
  assert.deepEqual(offline.requests.map(request => request.method), ['render.ready'])
})
