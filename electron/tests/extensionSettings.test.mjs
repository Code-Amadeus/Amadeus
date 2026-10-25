import assert from 'node:assert/strict'
import test from 'node:test'
import * as React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const catalog = loadTypeScript(new URL('../src/renderer/components/extensionSettingsCatalog.ts', import.meta.url))
const voiceCatalog = loadTypeScript(new URL('../src/renderer/components/voiceConfigurationCatalog.ts', import.meta.url))

test('Skill binding lists supported Agents and mixed packages require every native projection', () => {
  const providers = [
    { provider_id: 'codex', capabilities: { capability_projections: ['agent_skill', 'mcp_connection'] } },
    { provider_id: 'acp_local', capabilities: { capability_projections: ['agent_skill', 'mcp_connection'] } },
    { provider_id: 'pi', capabilities: { capability_projections: ['agent_skill'] } },
    { provider_id: 'browser', capabilities: { capability_projections: [] } },
  ]
  const selected = { manifest: { contributes: { skill: [{ id: 'handoff' }] } } }
  assert.deepEqual(catalog.compatibleExtensionProviders(providers, selected).map(p => p.provider_id), ['codex', 'acp_local', 'pi'])
  selected.manifest.contributes.mcp_server = [{ id: 'notes' }]
  assert.deepEqual(catalog.compatibleExtensionProviders(providers, selected).map(p => p.provider_id), ['codex', 'acp_local'])
})
const translate = (text, variables = {}) => Object.entries(variables).reduce((value, [key, item]) => value.replaceAll(`{${key}}`, String(item)), text)
const mocks = { '../i18n': { useI18n: () => ({ locale: 'en-US', t: translate }) }, './FluentIcon': { default: () => null } }
const extension = (overrides = {}) => ({
  id: 'org.example.voice', version: '1.0.0', revision: 1, digest: 'a'.repeat(64), enabled: true,
  pendingRemoval: false, status: 'enabled', restartRequired: false,
  values: { retries: 2, speed: 1.2, style: 'neutral', normalize: true }, secretConfigured: { token: true }, providers: ['pi'],
  manifest: { schema: 'amadeus.extension/v0', id: 'org.example.voice', version: '1.0.0', display_name: { en: 'Example voice', 'zh-CN': '示例语音' }, description: 'Synthetic test package',
    author: { name: 'Example author' }, engines: { amadeus_host_api: '>=0.1,<0.2' }, runtime: { kind: 'python' },
    contributes: { voice_backend: [{ id: 'speech', role: 'tts', contract: 'amadeus.voice.tts/v0', deployment: 'remote', languages: ['en'], streaming: true }],
      mcp_server: [{ id: 'tools', contract: 'amadeus.mcp-server/v0', transport: 'http', url: '${config.url}' }] },
    config: { token: { type: 'string', title: 'API token', secret: true, required: true },
      retries: { type: 'integer', title: 'Retries', default: 2, min: 0, max: 5 }, speed: { type: 'number', title: 'Speed', default: 1 },
      style: { type: 'enum', title: 'Style', options: ['neutral', { value: 'bright', label: { en: 'Bright' } }] },
      normalize: { type: 'boolean', title: 'Normalize', default: false }, url: { type: 'url', title: 'Endpoint' }, note: { type: 'string', title: 'Note', max_length: 120 } },
  }, ...overrides,
})
const providers = [
  { provider_id: 'pi', display_name: 'Pi', capabilities: { capability_projections: ['mcp_connection'] } },
  { provider_id: 'unrelated', display_name: 'Incompatible agent', capabilities: { capability_projections: ['skill'] } },
]
const props = overrides => ({ platform: 'win32', extensions: [extension()], runtime: [], providers, connected: false, available: true, encryptionAvailable: true, selectedVoiceBackend: '',
  onSettingsChanged() {}, onRestartRequired() {}, onOpenVoice() {}, ...overrides })

test('typed extension edits preserve defaults and never reconstruct saved secrets', () => {
  const saved = extension()
  const draft = catalog.extensionDraft(saved)
  assert.deepEqual(draft.secrets, {})
  assert.equal(draft.values.normalize, true)
  assert.deepEqual(catalog.extensionUpdate(saved, draft).values, {})
  draft.values.retries = '4'; draft.values.speed = '1.75'; draft.values.normalize = false; draft.values.style = 'bright'
  draft.values.note = 'explicit note'; draft.secrets.token = 'synthetic replacement'
  const update = catalog.extensionUpdate(saved, draft)
  assert.deepEqual(update.values, { retries: 4, speed: 1.75, style: 'bright', normalize: false, note: 'explicit note' })
  assert.deepEqual(update.secrets, { token: 'synthetic replacement' })
  draft.secrets.token = null
  assert.deepEqual(catalog.extensionUpdate(saved, draft).secrets, { token: null })
  const withOptional = extension({ values: { ...saved.values, note: 'saved note' } })
  const reset = catalog.extensionDraft(withOptional); reset.values.note = ''
  assert.equal(catalog.extensionUpdate(withOptional, reset).values.note, null)
})

test('unavailable metadata keeps identity visible, prevents new edits and preserves a selected voice as unavailable', () => {
  const unavailable = extension({ manifest: null, status: 'unavailable', restartRequired: true, values: {}, providers: [], secretConfigured: {}, validationError: 'Extension metadata is unavailable.' })
  assert.deepEqual(catalog.extensionVoiceOptions([unavailable]), [])
  assert.deepEqual(catalog.extensionVoiceOptions([unavailable], 'en-US', 'org.example.voice/speech'), [{ value: 'org.example.voice/speech', label: 'org.example.voice/speech', disabled: true, unavailable: true }])
  assert.throws(() => catalog.extensionUpdate(unavailable, catalog.extensionDraft(unavailable)), /metadata is unavailable/)
  const Component = loadTypeScript(new URL('../src/renderer/components/ExtensionSettings.tsx', import.meta.url), mocks).default
  const html = renderToStaticMarkup(React.createElement(Component, props({ extensions: [unavailable] })))
  assert.ok(html.includes('org.example.voice'))
  assert.ok(html.includes('1.0.0'))
  assert.ok(html.includes('Extension unavailable'))
  assert.ok(!html.includes('Configure extension'))
  assert.ok(!html.includes('Speech synthesis'))
})

test('installed extension voices are discoverable offline through the single TTS_BACKEND selector', () => {
  const enabled = extension()
  const disabled = extension({ id: 'org.example.disabled', enabled: false })
  const removed = extension({ id: 'org.example.removed', pendingRemoval: true })
  const groups = voiceCatalog.buildVoiceConfigurationCatalog({ asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: false }, {
    values: { TTS_BACKEND: `${enabled.id}/speech` }, extensions: [enabled, disabled, removed],
  })
  const selector = groups.find(group => group.id === 'speech_synthesis').fields.find(field => field.key === 'TTS_BACKEND')
  assert.equal(selector.value, 'org.example.voice/speech')
  assert.equal(selector.options.find(item => item.value === 'org.example.disabled/speech').disabled, true)
  assert.equal(selector.options.some(item => item.value === 'org.example.removed/speech'), false)
  assert.deepEqual(catalog.extensionVoiceOptions([], 'en-US', 'org.example.removed/speech'), [{ value: 'org.example.removed/speech', label: 'org.example.removed/speech', disabled: true, unavailable: true }])
  assert.equal(groups.flatMap(group => group.fields).filter(field => field.key === 'TTS_BACKEND').length, 1)
  assert.equal(catalog.extensionLanguageSupported(['en-US'], 'en'), true)
  assert.equal(catalog.extensionLanguageSupported(['en'], 'ja'), false)
  assert.deepEqual(catalog.compatibleExtensionProviders(providers).map(provider => provider.provider_id), ['pi'])
  assert.equal(catalog.extensionRuntimeLabel('ready'), 'Ready')
  assert.equal(catalog.extensionRuntimeLabel('degraded'), 'Degraded')
})

test('saved disable and removal remain distinct from observed active runtime; disconnected status is unavailable', () => {
  const Component = loadTypeScript(new URL('../src/renderer/components/ExtensionSettings.tsx', import.meta.url), mocks).default
  const runtime = [{ id: 'org.example.voice', version: '0.9.0', enabled: true, state: 'active', detail: 'Synthetic runtime', contributions: [] }]
  const render = input => renderToStaticMarkup(React.createElement(Component, props(input)))
  const disabled = render({ extensions: [extension({ enabled: false, restartRequired: true })], runtime, connected: true })
  for (const label of ['Saved state', 'Disabled', 'Current backend', 'Active', 'Restart required', 'Running version', '0.9.0']) assert.ok(disabled.includes(label), label)
  assert.ok(render({ extensions: [extension({ pendingRemoval: true, restartRequired: true })], runtime, connected: true }).includes('Removal pending restart'))
  const offline = render({ runtime })
  assert.ok(offline.includes('Backend status unavailable'))
  assert.ok(!offline.includes('Synthetic runtime'))
  assert.ok(offline.includes('Japanese output has not been verified'))
  assert.ok(!offline.includes('aria-label="Configure extension"'))
  assert.ok(!offline.includes('Grant'))
})

test('extension lifecycle and English-only compatibility use the real Chinese dictionary', t => {
  const previous = globalThis.localStorage
  globalThis.localStorage = { getItem: () => 'zh-CN' }
  t.after(() => { globalThis.localStorage = previous })
  const cache = new Map()
  const i18n = loadTypeScript(new URL('../src/renderer/i18n.tsx', import.meta.url), {}, cache)
  const Component = loadTypeScript(new URL('../src/renderer/components/ExtensionSettings.tsx', import.meta.url), { './FluentIcon': { default: () => null } }, cache).default
  const html = renderToStaticMarkup(React.createElement(i18n.I18nProvider, null, React.createElement(Component, props({ extensions: [extension({ pendingRemoval: true, restartRequired: true })] }))))
  for (const label of ['示例语音', '已保存状态', '当前后端', '重启后卸载', '安装插件', '日语输出尚未验证']) assert.ok(html.includes(label), label)
  assert.ok(!html.includes('Saved state'))
})

function harness(api, initialProps = {}) {
  const previous = globalThis.window
  globalThis.window = { amadeus: api, confirm: () => true }
  const slots = []; let cursor = 0; let tree; let effects = []; let cleanups = []; let currentProps = props(initialProps)
  const hooks = { ...React,
    useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
      return [slots[index], update => { slots[index] = typeof update === 'function' ? update(slots[index]) : update }] },
    useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index] },
    useEffect(callback) { effects.push(callback) },
  }
  const Component = loadTypeScript(new URL('../src/renderer/components/ExtensionSettings.tsx', import.meta.url), { ...mocks, react: hooks }).default
  const render = (update = {}) => { cleanups.forEach(cleanup => cleanup()); cleanups = []; currentProps = { ...currentProps, ...update }; cursor = 0; effects = []; tree = Component(currentProps); cleanups = effects.map(callback => callback()).filter(value => typeof value === 'function'); return tree }
  function* walk(node) { if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) { for (const item of node) yield* walk(item); return }
    yield node; yield* walk(node.props?.children); yield* walk(node.props?.action)
  }
  const text = node => Array.isArray(node) ? node.map(text).join('') : node && typeof node === 'object' ? text(node.props?.children) : String(node ?? '')
  render()
  return { render, find: predicate => [...walk(tree)].find(predicate), button: label => [...walk(tree)].find(node => node.props?.onClick && text(node) === label),
    restore() { cleanups.forEach(cleanup => cleanup()); globalThis.window = previous } }
}
const flush = () => new Promise(resolve => setImmediate(resolve))

test('editor preserves drafts across snapshots and failed saves, then submits typed edits and pending restart', async t => {
  let submitted, settings, restarts = 0, fail = true
  const page = harness({ updateExtension: async update => { submitted = update; return fail ? { ok: false, error: 'Synthetic save failure' } : { ok: true, settings: { restartRequired: true, extensions: [extension({ revision: 2, restartRequired: true })] } } } },
    { onSettingsChanged: value => { settings = value }, onRestartRequired: () => { restarts++ } })
  t.after(() => page.restore())
  page.button('Configure extension').props.onClick(); page.render()
  page.find(node => node.props?.id === 'extension-config-speed' && node.type === 'input').props.onChange({ target: { value: '1.75' } }); page.render()
  page.find(node => node.props?.id === 'extension-config-token' && node.type === 'input').props.onChange({ target: { value: 'synthetic secret' } }); page.render()
  page.render({ extensions: [extension({ values: { speed: 9 } })] })
  assert.equal(page.find(node => node.props?.id === 'extension-config-speed' && node.type === 'input').props.value, '1.75')
  page.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); page.render()
  assert.equal(page.find(node => node.props?.role === 'alert').props.children, 'Synthetic save failure')
  assert.equal(page.find(node => node.props?.id === 'extension-config-token' && node.type === 'input').props.value, 'synthetic secret')
  assert.equal(restarts, 0)
  fail = false
  page.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); page.render()
  assert.deepEqual(submitted.values, { speed: 1.75 })
  assert.deepEqual(submitted.secrets, { token: 'synthetic secret' })
  assert.equal(restarts, 1); assert.ok(settings.extensions[0].restartRequired)
  assert.equal(page.find(node => node.type === 'form'), undefined)
})

test('validator outage suspends a config editor and restores its unsubmitted draft after revalidation', t => {
  const page = harness({})
  t.after(() => page.restore())
  page.button('Configure extension').props.onClick(); page.render()
  page.find(node => node.props?.id === 'extension-config-note' && node.type === 'input').props.onChange({ target: { value: 'retained draft' } }); page.render()
  page.render({ extensions: [extension({ manifest: null, status: 'unavailable' })] })
  assert.equal(page.find(node => node.type === 'form'), undefined)
  assert.ok(page.find(node => node.props?.children === 'Extension metadata is unavailable. Your unsaved changes are retained until validation recovers.'))
  page.render({ extensions: [extension()] })
  assert.equal(page.find(node => node.props?.id === 'extension-config-note' && node.type === 'input').props.value, 'retained draft')
})

test('wheel cancellation retains a draft; compatible Provider choices and secret clear use explicit mutations', async t => {
  let installs = 0, removed = '', removalOptions
  const page = harness({ installExtension: async () => { installs++; return { ok: false, cancelled: true } }, removeExtension: async (id, options) => { removed = id; removalOptions = options; return { ok: true, settings: {} } } })
  t.after(() => page.restore())
  page.button('Configure extension').props.onClick(); page.render()
  page.find(node => node.props?.id === 'extension-config-note' && node.type === 'input').props.onChange({ target: { value: 'draft' } }); page.render()
  page.button('Install plugin…').props.onClick(); await flush(); page.render()
  assert.equal(installs, 1)
  assert.equal(page.find(node => node.props?.id === 'extension-config-note' && node.type === 'input').props.value, 'draft')
  assert.equal(page.find(node => node.type === 'label' && node.props.children?.[1] === 'Incompatible agent'), undefined)
  page.button('Clear API token').props.onClick(); page.render()
  assert.equal(page.find(node => node.props?.id === 'extension-config-token' && node.type === 'input').props.value, '')
  page.button('Cancel').props.onClick(); page.render()
  page.button('Remove').props.onClick(); page.render()
  assert.equal(removed, '')
  const deletion = page.find(node => node.type === 'input' && node.props.type === 'checkbox')
  assert.equal(deletion.props.checked, false)
  page.button('Confirm remove').props.onClick(); await flush(); page.render()
  assert.equal(removed, 'org.example.voice')
  assert.deepEqual(removalOptions, { deleteData: false })
})

test('a rejected update replaces previous success feedback without claiming it applied', async t => {
  let reject = false
  const page = harness({ installExtension: async () => reject
    ? { ok: false, error: 'Theme contrast is invalid' }
    : { ok: true, settings: { extensions: [extension({ revision: 2 })] } } })
  t.after(() => page.restore())
  page.button('Install plugin…').props.onClick(); await flush(); page.render()
  assert.ok(page.find(node => node.props?.role === 'status'))
  reject = true
  page.button('Install plugin…').props.onClick(); await flush(); page.render()
  assert.equal(page.find(node => node.props?.role === 'status'), undefined)
  assert.equal(page.find(node => node.props?.role === 'alert').props.children, 'Theme contrast is invalid')
})

test('a save receipt remains accurate when a later backend restart applies the package', async t => {
  const pending = extension({ revision: 2, appliedRevision: 1, restartRequired: true })
  const page = harness({ installExtension: async () => ({ ok: true, settings: { restartRequired: true, extensions: [pending] } }) })
  t.after(() => page.restore())
  page.button('Install plugin…').props.onClick(); await flush()
  page.render({ extensions: [pending] })
  const receipt = page.find(node => node.props?.role === 'status').props.children
  assert.equal(receipt, 'Saved. Current application status is shown on each package.')
  page.render({ extensions: [extension({ revision: 2, appliedRevision: 2, restartRequired: false })], connected: true,
    runtime: [{ id: 'org.example.voice', version: '1.0.0', state: 'active', contributions: [] }] })
  assert.equal(page.find(node => node.props?.role === 'status').props.children, receipt)
  assert.equal(page.find(node => node.props?.children === 'Restart required'), undefined)
})

test('removal requests data deletion only when explicitly selected', async t => {
  let options
  const page = harness({ removeExtension: async (_id, value) => { options = value; return { ok: true, settings: {} } } })
  t.after(() => page.restore())
  page.button('Remove').props.onClick(); page.render()
  page.find(node => node.type === 'input' && node.props.type === 'checkbox').props.onChange({ target: { checked: true } }); page.render()
  page.button('Confirm remove').props.onClick(); await flush(); page.render()
  assert.deepEqual(options, { deleteData: true })
})

const SELECTED_VOICE_WARNING = 'This package contains the saved speech engine. After restarting, speech is unavailable while text responses continue. Choose an enabled voice to restore speech.'
const cardSwitch = (page, id = 'org.example.voice') => page.find(node => node.props?.id === `extension-enabled-${id}` && node.props.onChange)

test('turning off the package that holds the saved voice asks first, and removal warns too', async t => {
  const updates = []
  const page = harness({ updateExtension: async update => { updates.push(update); return { ok: true, settings: {} } } },
    { selectedVoiceBackend: 'org.example.voice/speech' })
  t.after(() => page.restore())
  cardSwitch(page).props.onChange(false); page.render()
  assert.ok(page.find(node => node.props?.children === SELECTED_VOICE_WARNING))
  assert.deepEqual(updates, [], 'nothing is saved before confirming')
  page.button('Keep enabled').props.onClick(); page.render()
  assert.equal(page.find(node => node.props?.children === SELECTED_VOICE_WARNING), undefined)
  cardSwitch(page).props.onChange(false); page.render()
  page.button('Disable').props.onClick(); await flush(); page.render()
  assert.deepEqual(updates, [{ id: 'org.example.voice', enabled: false, values: {}, secrets: {}, providers: ['pi'] }])
  page.button('Remove').props.onClick(); page.render()
  assert.ok(page.find(node => node.props?.children === SELECTED_VOICE_WARNING))
})

test('the card switch changes only enabled, and a form save never sends enabled', async t => {
  const updates = []
  const page = harness({ updateExtension: async update => { updates.push(update); return { ok: true, settings: {} } } })
  t.after(() => page.restore())
  cardSwitch(page).props.onChange(false); await flush(); page.render()
  assert.deepEqual(updates, [{ id: 'org.example.voice', enabled: false, values: {}, secrets: {}, providers: ['pi'] }])
  page.button('Configure extension').props.onClick(); page.render()
  assert.ok(page.find(node => node.type === 'form'), 'the editor opens inside the package card')
  page.find(node => node.props?.id === 'extension-config-speed' && node.type === 'input').props.onChange({ target: { value: '1.75' } }); page.render()
  page.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); page.render()
  assert.equal('enabled' in updates[1], false, 'a switch flipped while the form was open stays as saved')
  assert.deepEqual(updates[1].values, { speed: 1.75 })
})

test('pending installation reports a busy navigation guard; failed installation leaves saved packages intact', async t => {
  let resolve; let state; let writes = 0
  const page = harness({ installExtension: () => new Promise(done => { resolve = done }) }, {
    onEditorStateChange: value => { state = value }, onSettingsChanged: () => { writes++ },
  })
  t.after(() => page.restore())
  assert.deepEqual(state, { dirty: false, busy: false })
  page.button('Install plugin…').props.onClick(); page.render()
  assert.deepEqual(state, { dirty: false, busy: true })
  assert.equal(page.button('Install plugin…'), undefined)
  resolve({ ok: false, error: 'Synthetic invalid wheel' }); await flush(); page.render()
  assert.equal(writes, 0)
  assert.equal(state.busy, false)
  assert.ok(page.button('Configure extension'))
  assert.equal(page.find(node => node.props?.role === 'alert').props.children, 'Synthetic invalid wheel')
})

test('catalog view switching retains a draft and blocked packages can be disabled but not activated', async t => {
  const updates=[]
  const page = harness({extensionCatalog: async () => ({ok:true,catalog:{entries:[],cached:false,installed:[{id:'org.example.voice',version:'1.0.0',digest:'a'.repeat(64),catalogState:'matched',blocked:true}]},settings:{extensionSecurity:{status:'ready'}}}),
    updateExtension: async update => { updates.push(update); return {ok:true,settings:{}} }})
  t.after(()=>page.restore())
  await flush();page.render()
  page.button('Configure extension').props.onClick();page.render()
  const value=()=>page.find(n=>n.props?.id==='extension-config-speed'&&n.type==='input')
  value().props.onChange({target:{value:'1.75'}});page.render()
  const toggle=()=>page.find(n=>n.props?.id==='extension-enabled-org.example.voice')
  assert.equal(toggle().props.disabled,false)
  toggle().props.onChange(false);await flush();page.render({extensions:[extension({enabled:false})]})
  assert.equal(updates[0].enabled,false)
  assert.equal(toggle().props.disabled,true)
  page.button('Browse').props.onClick();await flush();page.render()
  page.button('Installed').props.onClick();page.render()
  assert.equal(value().props.value,'1.75')
  assert.equal(toggle().props.checked,false)
})

test('opening Browse cannot implicitly recover damaged safety state', async t => {
  const requests=[]
  const page=harness({extensionCatalog:async request=>{requests.push(request);return {ok:true,catalog:{entries:[],cached:true},settings:{extensionSecurity:{status:'unavailable'}}}}})
  t.after(()=>page.restore())
  await flush();page.render()
  page.button('Browse').props.onClick();await flush();page.render()
  assert.equal(requests.filter(r=>r.action==='recover'||r.action==='refresh').length,0)
  page.button('Recover security state').props.onClick();await flush();page.render()
  assert.equal(requests.filter(r=>r.action==='recover').length,1)
})


test('update switch changes only checking preference and exact version scheduling never requests hot installation',async t=>{
  const requests=[]
  const candidate={id:'org.example.voice',version:'1.1.0',sha256:'b'.repeat(64),size:1,url:'https://example.org/plugin.whl',display:{},compatibility:{contributions:[]},review:{},withdrawal:null,incompatible:[]}
  const view={entries:[candidate],cached:false,autoCheck:false}
  const page=harness({extensionCatalog:async request=>{
    requests.push(request)
    if(request.action==='auto-check')view.autoCheck=request.enabled
    return {ok:true,scheduled:request.action==='schedule',catalog:{...view}}
  }})
  t.after(()=>page.restore())
  await flush();page.render()
  page.find(node=>node.props?.label==='Automatic plugin update checks'&&node.props.onChange).props.onChange(true)
  await flush();page.render()
  assert.deepEqual(requests.find(request=>request.action==='auto-check'),{action:'auto-check',enabled:true})
  page.button('Schedule update for next app start…').props.onClick()
  await flush();page.render()
  assert.deepEqual(requests.find(request=>request.action==='schedule'),{action:'schedule',id:'org.example.voice',version:'1.1.0'})
  assert.equal(requests.some(request=>request.action==='install'),false)
  assert.ok(page.find(node=>node.props?.role==='status'&&String(node.props.children).includes('Installed packages have not changed')))
})

test('unconfirmed update exposes rollback and status refresh while keep remains blocked',async t=>{
  const requests=[]
  const page=harness({extensionCatalog:async request=>{requests.push(request);return {ok:true,scheduled:request.action==='rollback',catalog:{entries:[],cached:true,pendingUpdate:{id:'org.example.voice',version:'1.1.0',previousVersion:'1.0.0',canConfirm:false,recoveryRequired:true}}}}})
  t.after(()=>page.restore())
  await flush();page.render()
  assert.equal(page.button('Keep this version').props.disabled,true)
  page.button('Check status').props.onClick();await flush();page.render()
  page.button('Schedule rollback…').props.onClick();await flush();page.render()
  assert.ok(requests.some(request=>request.action==='health'))
  assert.ok(requests.some(request=>request.action==='rollback'))
  assert.equal(requests.some(request=>request.action==='install'||request.action==='confirm-update'),false)
})


test('native local import reports a queued change instead of claiming an installed package', async t => {
  let writes = 0
  const page = harness({installExtension: async () => ({ok:true,scheduled:true})}, {onSettingsChanged: () => { writes++ }})
  t.after(() => page.restore())
  page.button('Install plugin…').props.onClick()
  await flush();page.render()
  assert.equal(writes, 0)
  assert.ok(page.find(node => node.props?.role === 'status' && String(node.props.children).includes('Scheduled for the next app start')))
})

test('update checking has an associated label and description, with manual checking kept independent', async t => {
  const requests = []
  const page = harness({extensionCatalog: async request => {
    requests.push(request)
    return {ok:true,catalog:{entries:[],cached:false,autoCheck:false},settings:{extensionSecurity:{status:'ready'}}}
  }})
  t.after(() => page.restore())
  await flush(); page.render()
  const toggle = page.find(node => node.props?.label === 'Automatic plugin update checks' && node.props.onChange)
  assert.ok(page.find(node => node.type === 'label' && node.props.htmlFor === toggle.props.id))
  assert.ok(page.find(node => node.props?.id === toggle.props.describedBy))
  page.button('Check now').props.onClick()
  await flush(); page.render()
  assert.equal(requests.filter(request => request.action === 'refresh').length, 1)
  assert.equal(requests.some(request => request.action === 'auto-check'), false)
})

test('scheduled installation keeps the full digest in closed details and cancellation remains available', async t => {
  const requests = []
  const digest = 'b'.repeat(64)
  const page = harness({extensionCatalog: async request => {
    requests.push(request)
    return {ok:true,catalog:{entries:[],cached:false,scheduledInstall:{id:'org.example.voice',version:'1.1.0',digest}}}
  }})
  t.after(() => page.restore())
  await flush(); page.render()
  const details = page.find(node => node.type === 'details' && node.props.children?.[0]?.props.children === 'Package details')
  assert.ok(details)
  assert.ok(!details.props.open)
  assert.ok(JSON.stringify(details.props.children).includes(digest))
  page.button('Cancel scheduled change').props.onClick()
  await flush(); page.render()
  assert.equal(requests.filter(request => request.action === 'cancel-scheduled').length, 1)
  assert.equal(requests.some(request => request.action === 'install' || request.action === 'confirm-update'), false)
})

test('a verified update still requires the explicit keep action', async t => {
  const requests = []
  const page = harness({extensionCatalog: async request => {
    requests.push(request)
    return {ok:true,catalog:{entries:[],cached:false,pendingUpdate:{id:'org.example.voice',version:'1.1.0',previousVersion:'1.0.0',canConfirm:true,recoveryRequired:false}}}
  }})
  t.after(() => page.restore())
  await flush(); page.render()
  assert.equal(requests.some(request => request.action === 'confirm-update'), false)
  assert.equal(page.button('Keep this version').props.disabled, false)
  page.button('Keep this version').props.onClick()
  await flush(); page.render()
  assert.equal(requests.filter(request => request.action === 'confirm-update').length, 1)
})

for (const platform of ['linux', 'darwin']) {
  test(`${platform} update UI requests immediate installation and reports its actual settings`, async t => {
    const requests = [], saved = []
    const candidate = {id:'org.example.voice',version:'1.1.0',sha256:'b'.repeat(64),size:1,url:'https://example.org/plugin.whl',display:{},compatibility:{contributions:[]},review:{},withdrawal:null,incompatible:[]}
    const page = harness({extensionCatalog: async request => {
      requests.push(request)
      return {ok:true,catalog:{entries:[candidate],cached:false}, ...(request.action === 'install' ? {settings:{extensions:['installed']}} : {})}
    }}, {platform, onSettingsChanged: value => saved.push(value)})
    t.after(() => page.restore())
    await flush(); page.render()
    assert.equal(page.button('Schedule update for next app start…'), undefined)
    page.button('Review and update…').props.onClick()
    await flush(); page.render()
    assert.deepEqual(requests.find(request => request.action === 'install'), {action:'install',id:candidate.id,version:candidate.version})
    assert.equal(requests.some(request => request.action === 'schedule'), false)
    assert.ok(saved.some(value => value.extensions?.[0] === 'installed'))
  })
}
