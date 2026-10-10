import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const definition = JSON.parse(fs.readFileSync(new URL('../../config/catalog/tts/fish_audio.json', import.meta.url), 'utf8'))
const catalog = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))
const credentials = {
  isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(`encrypted:${value}`),
  decryptString: value => value.toString().slice(10),
}
function store(t, overrides = {}) {
  const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), {
    electron: { safeStorage: credentials }, ...overrides,
  })
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-catalog-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  return new DesktopSettingsStore(path.join(directory, 'settings.json'), path.join(directory, '.env'))
}

test('committed Electron catalog and env examples match the canonical JSON', () => {
  execFileSync(process.execPath, [fileURLToPath(new URL('../scripts/generate-config-catalog.mjs', import.meta.url)), '--check'])
  const directory = new URL('../../config/catalog/', import.meta.url)
  const declarations = fs.readdirSync(directory, { recursive: true }).filter(name => name.endsWith('.json')).sort()
    .map(name => JSON.parse(fs.readFileSync(new URL(name.replaceAll('\\', '/'), directory), 'utf8')))
  assert.deepEqual(catalog.catalogGroups, declarations)
})

test('offline fields, translations and desktop persistence consume every declared Fish field', t => {
  const settings = store(t)
  const offline = catalog.catalogConfiguration(definition.id)
  assert.equal(offline.label, definition.title['en-US'])
  for (const [key, field] of Object.entries(definition.config)) {
    const control = offline.fields.find(item => item.key === key)
    assert.equal(control.label, field.title['en-US'])
    assert.equal(catalog.catalogTranslations[control.label], field.title['zh-CN'])
    assert.equal(control.restart_required, true)
    if (field.secret) {
      assert.equal(control.type, 'secret')
      assert.equal(control.configured, false)
      assert.throws(() => settings.update({}, { values: { [key]: 'credential' } }), /Unsupported desktop setting/)
    } else {
      assert.equal(control.value, field.default)
      settings.update({}, { values: { [key]: field.default } })
      assert.equal(settings.backendEnvironment({})[key], field.default)
      assert.throws(() => settings.update({}, { secrets: { [key]: 'credential' } }), /Unsupported desktop secret/)
    }
  }
})

test('all migrated controls share defaults, validation, translations and storage ownership', t => {
  const settings = store(t)
  for (const group of catalog.catalogGroups) {
    const computed = Object.fromEntries(Object.entries(group.config).filter(([, field]) => field.computed_default)
      .map(([key, field]) => [key, field.example]))
    const controls = catalog.catalogConfiguration(group.id, null, computed).fields
    for (const [key, field] of Object.entries(group.config)) {
      const control = controls.find(item => item.key === key)
      assert.equal(catalog.catalogTranslations[control.label], field.title['zh-CN'])
      if (field.secret) {
        settings.update({}, { secrets: { [key]: 'test-credential' } })
        assert.equal(settings.snapshot({}).secrets[key].configured, true)
        assert.ok(!JSON.stringify(settings.snapshot({})).includes('test-credential'))
        assert.throws(() => settings.update({}, { values: { [key]: 'test-credential' } }))
      } else {
        const value = field.computed_default ? field.example : field.default
        assert.equal(control.value, typeof value === 'boolean' ? value : String(value))
        settings.update({}, { values: { [key]: typeof value === 'number' ? String(value) : value } })
        if (value !== '') assert.equal(settings.backendEnvironment({})[key], String(value))
        if (field.options) assert.throws(() => settings.update({}, { values: { [key]: 'unlisted-choice' } }))
      }
    }
  }
})

test('a new declared TTS backend appears in selection, its section and durable settings', t => {
  const group = structuredClone(definition)
  group.id = 'tts_catalog_fixture'
  group.voice_backend.id = 'catalog_fixture'
  group.voice_backend.order = 100
  group.config = { CATALOG_FIXTURE_MODEL: {
    type: 'string', title: { 'en-US': 'Fixture model', 'zh-CN': '测试模型' }, default: 'fixture-default',
  } }
  const overrides = { './configCatalog.generated.js': { catalogGroups: [...catalog.catalogGroups, group] } }
  const voice = loadTypeScript(new URL('../src/renderer/components/voiceConfigurationCatalog.ts', import.meta.url), overrides)
  const settings = store(t, overrides)
  const saved = settings.update({}, { values: { TTS_BACKEND: 'catalog_fixture', CATALOG_FIXTURE_MODEL: 'my-model' } })
  const groups = voice.buildVoiceConfigurationCatalog({ asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: true }, saved)
  assert.ok(groups.find(item => item.id === 'speech_synthesis').fields[0].options.some(option => option.value === 'catalog_fixture'))
  assert.ok(voice.voiceConfigurationSections.remote.has(group.id))
  assert.equal(groups.find(item => item.id === group.id).fields[0].value, 'my-model')
  assert.equal(settings.backendEnvironment({}).CATALOG_FIXTURE_MODEL, 'my-model')
})

test('a new declared field reaches the offline form and durable store without owner edits', t => {
  const group = structuredClone(definition)
  group.config.TEST_VOICE_STYLE = {
    type: 'enum', title: { 'en-US': 'Voice style', 'zh-CN': '语音风格' },
    default: 'natural', options: ['natural', 'calm'],
  }
  const overrides = { './configCatalog.generated.js': { catalogGroups: [group] } }
  const settings = store(t, overrides)
  const projection = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url), overrides)
  assert.equal(projection.catalogConfiguration(group.id).fields.at(-1).value, 'natural')
  const saved = settings.update({}, { values: { TEST_VOICE_STYLE: 'calm' } })
  assert.equal(projection.catalogConfiguration(group.id, saved).fields.at(-1).value, 'calm')
  assert.equal(settings.backendEnvironment({}).TEST_VOICE_STYLE, 'calm')
  assert.throws(() => settings.update({}, { values: { TEST_VOICE_STYLE: 'invalid' } }))
  assert.throws(() => settings.update({}, { values: { UNDECLARED_VOICE_STYLE: 'calm' } }))
})

test('Fish keeps environment authority, clearing and revision-aware application', t => {
  const settings = store(t)
  const first = settings.update({}, { values: { FISH_TTS_MODEL: 'first-model' }, secrets: { FISH_TTS_API_KEY: 'credential' } })
  const second = settings.update({}, { values: { FISH_TTS_MODEL: 'second-model' } })
  const stale = settings.markApplied({}, first.pendingRevisions)
  assert.equal(stale.pendingRevisions.FISH_TTS_MODEL, second.pendingRevisions.FISH_TTS_MODEL)
  const environment = { FISH_TTS_MODEL: 'process-model', FISH_TTS_API_KEY: 'process-key' }
  assert.equal(settings.snapshot(environment).sources.FISH_TTS_MODEL, 'environment')
  assert.equal(settings.snapshot(environment).locked.FISH_TTS_API_KEY, true)
  assert.ok(!('FISH_TTS_MODEL' in settings.backendEnvironment(environment)))
  assert.ok(!('FISH_TTS_API_KEY' in settings.backendEnvironment(environment)))
  assert.throws(() => settings.update(environment, { values: { FISH_TTS_MODEL: 'changed' } }), /locked/)
  assert.throws(() => settings.update(environment, { secrets: { FISH_TTS_API_KEY: 'changed' } }), /locked/)
  const cleared = settings.update({}, { values: { FISH_TTS_MODEL: null }, secrets: { FISH_TTS_API_KEY: null } })
  assert.equal(cleared.secrets.FISH_TTS_API_KEY.configured, false)
  assert.ok(!('FISH_TTS_MODEL' in settings.backendEnvironment({})))
  assert.equal(catalog.catalogConfiguration(definition.id, cleared).fields.find(field => field.key === 'FISH_TTS_MODEL').value,
    definition.config.FISH_TTS_MODEL.default)
})

test('missing credential encryption rejects Fish secret without a partial save', t => {
  const settings = store(t, { electron: { safeStorage: { isEncryptionAvailable: () => false } } })
  assert.throws(() => settings.update({}, {
    values: { FISH_TTS_MODEL: 'unsaved-model' }, secrets: { FISH_TTS_API_KEY: 'credential' },
  }), /encryption is unavailable/)
  assert.ok(!('FISH_TTS_MODEL' in settings.snapshot({}).values))
  assert.equal(settings.snapshot({}).secrets.FISH_TTS_API_KEY.configured, false)
})

test('generation rejects stale output and invalid declarations before they reach a build', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-catalog-generation-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.mkdirSync(path.join(root, 'electron/scripts'), { recursive: true })
  fs.mkdirSync(path.join(root, 'config/catalog/tts'), { recursive: true })
  const script = path.join(root, 'electron/scripts/generate-config-catalog.mjs')
  fs.copyFileSync(new URL('../scripts/generate-config-catalog.mjs', import.meta.url), script)
  const declaration = path.join(root, 'config/catalog/tts/fish_audio.json')
  const write = group => fs.writeFileSync(declaration, JSON.stringify(group))
  write(definition)
  fs.writeFileSync(path.join(root, '.env.example'), '# BEGIN GENERATED CONFIG: tts_fish_audio\n# END GENERATED CONFIG: tts_fish_audio\n')
  execFileSync(process.execPath, [script])
  execFileSync(process.execPath, [script, '--check'])
  const changed = structuredClone(definition)
  changed.config.FISH_TTS_MODEL.default = 'new-model'
  write(changed)
  assert.throws(() => execFileSync(process.execPath, [script, '--check'], { stdio: 'pipe' }), /stale/)
  changed.config.FISH_TTS_API_KEY.default = 'forbidden-secret-default'
  write(changed)
  assert.throws(() => execFileSync(process.execPath, [script], { stdio: 'pipe' }), /must not declare a default/)
  delete changed.config.FISH_TTS_API_KEY.default
  changed.config.FISH_TTS_MODEL.defaut = 'misspelled'
  write(changed)
  assert.throws(() => execFileSync(process.execPath, [script], { stdio: 'pipe' }), /Unknown declaration property/)
  write(definition)
  const envPath = path.join(root, '.env.example')
  const current = fs.readFileSync(envPath, 'utf8')
  fs.writeFileSync(envPath, current + '\n# FISH_TTS_MODEL=duplicate\n')
  assert.throws(() => execFileSync(process.execPath, [script], { stdio: 'pipe' }), /duplicated outside/)
  fs.writeFileSync(envPath, current)
  const renamed = structuredClone(definition)
  renamed.id = 'renamed_group'
  write(renamed)
  execFileSync(process.execPath, [script])
  const renamedEnv = fs.readFileSync(envPath, 'utf8')
  assert.ok(!renamedEnv.includes('GENERATED CONFIG: tts_fish_audio'))
  assert.equal((renamedEnv.match(/FISH_TTS_MODEL=/g) || []).length, 1)
  fs.unlinkSync(declaration)
  execFileSync(process.execPath, [script])
  assert.ok(!fs.readFileSync(envPath, 'utf8').includes('FISH_TTS_MODEL='))
  fs.writeFileSync(envPath, '# BEGIN GENERATED CONFIG: broken\n')
  assert.throws(() => execFileSync(process.execPath, [script], { stdio: 'pipe' }), /Unpaired/)
})

test('LM Studio legacy inputs have the same authority, locking and canonical form value', t => {
  const settings = store(t)
  settings.update({}, { values: { LM_STUDIO_URL: 'http://localhost:1235', LOCAL_LLM_LM_STUDIO_URL: 'http://localhost:1236' } })
  const environment = { LM_STUDIO_URL: 'http://localhost:1237' }
  const snapshot = settings.snapshot(environment)
  assert.equal(snapshot.sources.LOCAL_LLM_LM_STUDIO_URL, 'environment')
  assert.equal(snapshot.locked.LOCAL_LLM_LM_STUDIO_URL, true)
  assert.equal(catalog.catalogConfiguration('local', snapshot).fields.find(field => field.key === 'LOCAL_LLM_LM_STUDIO_URL').value, environment.LM_STUDIO_URL)
  assert.equal(settings.backendEnvironment(environment).LOCAL_LLM_LM_STUDIO_URL, environment.LM_STUDIO_URL)
  assert.throws(() => settings.update(environment, { values: { LOCAL_LLM_LM_STUDIO_URL: 'http://localhost:1238' } }), /locked/)
  const canonical = { ...environment, LOCAL_LLM_LM_STUDIO_URL: 'http://localhost:1239' }
  assert.equal(settings.snapshot(canonical).startupValues.LOCAL_LLM_LM_STUDIO_URL, canonical.LOCAL_LLM_LM_STUDIO_URL)
  assert.ok(!('LOCAL_LLM_LM_STUDIO_URL' in settings.backendEnvironment(canonical)))
  const cleared = settings.update({}, { values: { LOCAL_LLM_LM_STUDIO_URL: null } })
  assert.equal(cleared.sources.LOCAL_LLM_LM_STUDIO_URL, 'default')
  assert.ok(!('LM_STUDIO_URL' in cleared.values))
  settings.markApplied({})
  settings.update({}, { values: { LM_STUDIO_URL: 'http://localhost:1240' } })
  settings.markApplied({})
  const legacyClear = settings.update({}, { values: { LOCAL_LLM_LM_STUDIO_URL: null } })
  assert.ok(legacyClear.pendingRevisions.LOCAL_LLM_LM_STUDIO_URL)
})
