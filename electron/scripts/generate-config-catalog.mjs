import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const catalogRoot = path.join(root, 'config/catalog')
const check = process.argv.includes('--check')
const files = fs.readdirSync(catalogRoot, { recursive: true }).filter(name => name.endsWith('.json')).sort()
const groups = files.map(name => JSON.parse(fs.readFileSync(path.join(catalogRoot, name), 'utf8')))
const ids = new Set()
const backendIds = new Set()
const keys = new Set()
const translations = {}
function knownKeys(value, allowed) {
  for (const key of Object.keys(value)) assert.ok(allowed.includes(key), `Unknown declaration property ${key}`)
}
function localized(text) {
  knownKeys(text, ['en-US', 'zh-CN'])
  for (const locale of ['en-US', 'zh-CN']) assert.equal(typeof text?.[locale], 'string', `Missing ${locale} text`)
  assert.ok(text['en-US'] && text['zh-CN'], 'Empty localized text')
  assert.ok(!translations[text['en-US']] || translations[text['en-US']] === text['zh-CN'], `Conflicting translation: ${text['en-US']}`)
  translations[text['en-US']] = text['zh-CN']
}
for (const group of groups) {
  knownKeys(group, ['id', 'title', 'description', 'desktop', 'restart_required', 'config', 'section', 'voice_backend', 'order'])
  assert.match(group.id, /^[a-z][a-z0-9_]*$/)
  assert.ok(!ids.has(group.id), `Duplicate group ${group.id}`)
  ids.add(group.id)
  localized(group.title)
  localized(group.description)
  if (group.section) assert.ok(['output', 'remote', 'input', 'roles', 'providers', 'routing'].includes(group.section))
  if (group.order !== undefined) assert.ok(Number.isInteger(group.order))
  if (group.voice_backend) {
    const backend = group.voice_backend
    knownKeys(backend, ['id', 'label', 'deployment', 'factory', 'probe', 'summary', 'order', 'streaming', 'reference_conditioning'])
    assert.match(backend.id, /^[a-z][a-z0-9_]*$/)
    assert.ok(backend.id !== 'disabled' && !backendIds.has(backend.id), `Duplicate/reserved voice backend ${backend.id}`)
    backendIds.add(backend.id)
    localized(backend.label)
    assert.ok(['embedded', 'remote'].includes(backend.deployment))
    assert.ok(group.section)
    assert.ok(Number.isInteger(backend.order))
    assert.equal(typeof backend.summary, 'string')
    assert.equal(typeof backend.reference_conditioning, 'boolean')
    const entry = /^tts\.backends\.[a-z_]+:[A-Za-z_]\w*$/
    assert.match(backend.factory, entry)
    assert.match(backend.probe, entry)
    if (typeof backend.streaming !== 'boolean') assert.match(backend.streaming, entry)
  }
  assert.equal(typeof group.desktop, 'boolean')
  // The initial catalog owns startup fields only; live application has a separate owner.
  assert.equal(group.restart_required, true)
  assert.ok(Object.keys(group.config).length, 'Empty configuration group')
  for (const [key, field] of Object.entries(group.config)) {
    knownKeys(field, ['type', 'title', 'description', 'default', 'secret', 'options', 'schemes', 'min', 'max', 'step', 'computed_default', 'example', 'example_active', 'accepted_values', 'aliases', 'setting', 'control', 'local_engines', 'scope', 'visible_when'])
    assert.match(key, /^[A-Z][A-Z0-9_]*$/)
    assert.ok(!keys.has(key), `Duplicate setting ${key}`)
    keys.add(key)
    if (field.setting) assert.match(field.setting, /^[A-Za-z_]\w*$/)
    if (field.aliases) for (const alias of field.aliases) {
      assert.match(alias, /^[A-Z][A-Z0-9_]*$/)
      assert.ok(!keys.has(alias), `Duplicate setting alias ${alias}`)
      keys.add(alias)
    }
    if (field.control) assert.ok(field.type === 'string' && ['number', 'select'].includes(field.control))
    if (field.scope) assert.ok(['backend', 'session', 'virtual', 'desktop'].includes(field.scope))
    if (field.visible_when) for (const [selector, choices] of Object.entries(field.visible_when)) {
      assert.ok(selector in group.config, `Unknown visibility selector ${selector}`)
      assert.ok(Array.isArray(choices) && choices.length && choices.every(value => typeof value === 'string'))
    }
    if (field.local_engines) assert.ok(group.id === 'local' && field.local_engines.every(engine => ['llama_server', 'lmstudio', 'ollama', 'cli'].includes(engine)))
    localized(field.title)
    if (field.description) localized(field.description)
    assert.ok(['string', 'path', 'url', 'enum', 'boolean', 'integer', 'number'].includes(field.type), `Unsupported type for ${key}`)
    if ('secret' in field) assert.equal(typeof field.secret, 'boolean')
    if ('options' in field) assert.ok(field.type === 'enum' || field.control === 'select')
    if ('schemes' in field) assert.equal(field.type, 'url')
    if (field.accepted_values) {
      assert.equal(field.type, 'boolean')
      assert.ok(field.accepted_values.every(value => ['true', 'false', '1', '0', 'yes', 'no'].includes(value)))
    }
    if (field.secret) {
      assert.equal(field.type, 'string')
      assert.ok(!('default' in field), `Secret ${key} must not declare a default`)
      assert.ok(!('example' in field) && !field.computed_default, `Secret ${key} must not contain example/default values`)
    } else if (field.computed_default) {
      assert.equal(field.computed_default, true)
      assert.ok(!('default' in field), `Computed ${key} must not declare a static default`)
      assert.ok('example' in field, `Computed ${key} needs an env example`)
    } else {
      const type = ['integer', 'number'].includes(field.type) ? 'number' : field.type === 'boolean' ? 'boolean' : 'string'
      assert.equal(typeof field.default, type, `Missing/invalid static default for ${key}`)
    }
    if (field.example_active !== undefined) assert.equal(typeof field.example_active, 'boolean')
    for (const value of [field.default, field.example].filter(value => value !== undefined)) {
      assert.ok(!/[\r\n\0]/.test(String(value)), `Invalid env example for ${key}`)
      if (field.type === 'boolean') assert.equal(typeof value, 'boolean')
      if (['integer', 'number'].includes(field.type)) {
        assert.ok(Number.isFinite(value) && (field.min === undefined || value >= field.min) && (field.max === undefined || value <= field.max), `Invalid numeric default/example for ${key}`)
        if (field.type === 'integer') assert.ok(Number.isInteger(value))
      }
    }
    if (['min', 'max', 'step'].some(name => name in field)) {
      assert.ok(['integer', 'number'].includes(field.type) || field.control === 'number')
      if (field.min !== undefined) assert.ok(Number.isFinite(field.min))
      if (field.max !== undefined) assert.ok(Number.isFinite(field.max))
      if (field.min !== undefined && field.max !== undefined) assert.ok(field.min <= field.max)
      assert.ok(Number.isFinite(field.step) && field.step > 0)
    }
    if (field.type === 'enum' || field.control === 'select' && field.options) {
      assert.ok(Array.isArray(field.options) && field.options.length)
      const values = field.options.map(option => {
        if (typeof option === 'string') return option
        knownKeys(option, ['value', 'label'])
        localized(option.label)
        assert.equal(typeof option.value, 'string')
        return option.value
      })
      assert.equal(new Set(values).size, values.length)
      assert.ok(values.includes(field.default), `Invalid enum default for ${key}`)
      if (field.example !== undefined) assert.ok(values.includes(field.example))
    }
    if (field.type === 'url') {
      assert.ok(Array.isArray(field.schemes) && field.schemes.length)
      assert.ok(field.schemes.every(value => ['http', 'https', 'ws', 'wss'].includes(value)))
      for (const value of [field.default, field.example].filter(value => value !== undefined && value !== '')) {
        assert.ok(field.schemes.includes(new URL(value).protocol.slice(0, -1)), `Invalid URL default for ${key}`)
      }
    }
  }
}

function output(relative, expected) {
  const target = path.join(root, relative)
  const current = fs.existsSync(target) ? fs.readFileSync(target, 'utf8').replaceAll('\r\n', '\n') : ''
  if (current === expected) return
  if (check) throw new Error(`${relative} is stale; run npm run generate:config in electron`)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.writeFileSync(target, expected)
}

const envPath = path.join(root, '.env.example')
let env = fs.readFileSync(envPath, 'utf8').replaceAll('\r\n', '\n')
const seen = new Set()
const block = /^# BEGIN GENERATED CONFIG: ([a-z][a-z0-9_]*)\n[\s\S]*?^# END GENERATED CONFIG: \1(?:\n|$)/gm
const markers = env.match(/^# (?:BEGIN|END) GENERATED CONFIG:.*$/gm) || []
const matches = [...env.matchAll(block)]
assert.equal(matches.length * 2, markers.length, 'Unpaired or nested .env.example markers')
function renderGroup(group) {
  const lines = [`# BEGIN GENERATED CONFIG: ${group.id}`, `# ${group.title['en-US']}`]
  for (const [key, field] of Object.entries(group.config)) {
    if (['virtual', 'desktop'].includes(field.scope)) continue
    const value = field.secret ? '<your-api-key>' : field.example ?? field.default
    const rendered = typeof value === 'string' && /[\s#"'\\]/.test(value) ? JSON.stringify(value) : String(value)
    const options = field.options ? `  # ${field.options.map(option => typeof option === 'string' ? option : option.value).join(' | ')}` : ''
    lines.push(`${field.example_active ? '' : '# '}${key}=${rendered}${options}`)
  }
  return [...lines, `# END GENERATED CONFIG: ${group.id}`, ''].join('\n')
}
const outside = env.replace(block, '')
for (const [, key] of outside.matchAll(/^\s*(?:#\s*)?(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=/gm)) {
  assert.ok(!keys.has(key), `Declared setting ${key} duplicated outside generated sections`)
}
env = env.replace(block, (_, id) => {
  assert.ok(!seen.has(id), `Duplicate .env.example section ${id}`)
  seen.add(id)
  const group = groups.find(group => group.id === id)
  return group ? renderGroup(group) : ''
})
for (const group of groups) if (!seen.has(group.id)) env += `\n${renderGroup(group)}`
output('electron/src/shared/configCatalog.generated.ts',
  '// Generated from config/catalog/**/*.json. Run npm run generate:config; do not edit.\n'
  + "import type { CatalogGroup } from './configCatalog.js'\n"
  + `export const catalogGroups: CatalogGroup[] = ${JSON.stringify(groups, null, 2)}\n`)

output('.env.example', env)
