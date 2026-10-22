import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const localeURL = new URL('../src/renderer/locales/zh-CN.json', import.meta.url)
const raw = fs.readFileSync(localeURL, 'utf8')
const manual = JSON.parse(raw)
const { catalogTranslations } = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))
const merged = { ...catalogTranslations, ...manual }

test('locale JSON has unique sorted keys and preserves substitution variables', () => {
  const source = ts.parseJsonText('zh-CN.json', raw)
  assert.equal(source.parseDiagnostics.length, 0)
  const keys = []
  function visit(node) {
    if (ts.isPropertyAssignment(node)) keys.push(node.name.text)
    ts.forEachChild(node, visit)
  }
  visit(source)
  assert.equal(new Set(keys).size, keys.length, 'Duplicate JSON key')
  assert.deepEqual(keys, [...keys].sort())
  const variables = value => [...new Set([...value.matchAll(/\{([^{}]+)\}/g)].map(match => match[1]))].sort()
  for (const [key, translation] of Object.entries(merged)) {
    assert.equal(typeof translation, 'string', key)
    assert.deepEqual(variables(translation), variables(key), key)
  }
})

test('built-in settings and interface translations never disagree', () => {
  for (const key of Object.keys(manual)) {
    if (Object.hasOwn(catalogTranslations, key)) assert.equal(manual[key], catalogTranslations[key], key)
  }
})

// Keys a t() argument can evaluate to: a literal, or the literal branches of a conditional or fallback.
function literalKeys(node) {
  if (ts.isParenthesizedExpression(node)) return literalKeys(node.expression)
  if (ts.isStringLiteralLike(node)) return [node.text]
  if (ts.isConditionalExpression(node)) return [...literalKeys(node.whenTrue), ...literalKeys(node.whenFalse)]
  if (ts.isBinaryExpression(node) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(node.operatorToken.kind)) {
    return [...literalKeys(node.left), ...literalKeys(node.right)]
  }
  return []
}

test('every literal renderer translation key has a translated entry', () => {
  const missing = []
  function scan(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, item.name)
      if (item.isDirectory()) { scan(filename); continue }
      if (!/\.tsx?$/.test(filename)) continue
      const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true)
      function visit(node) {
        if (ts.isCallExpression(node) && node.arguments.length && (
          ts.isIdentifier(node.expression) && node.expression.text === 't'
          || ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 't'
        )) {
          for (const key of literalKeys(node.arguments[0])) {
            if (!Object.hasOwn(merged, key)) missing.push(`${filename}: ${key}`)
          }
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }
  }
  scan(fileURLToPath(new URL('../src/renderer', import.meta.url)))
  assert.deepEqual(missing, [], 'Add translations for literal t() calls')
})

// Identifiers (backend and model ids) and product names are shown as written.
const shownAsWritten = value => /^[a-z0-9][a-z0-9_.-]*(, [a-z0-9][a-z0-9_.-]*)*$/.test(value)
  || ['DeepSeek', 'Gemini', 'AWS Bedrock'].includes(value)

test('capability projection text has a translated entry in every configuration', () => {
  const { buildCapabilityProfiles } = loadTypeScript(new URL('../src/renderer/components/sceneCapabilityProjection.ts', import.meta.url))
  const missing = new Set()
  const need = value => { if (value && !shownAsWritten(value) && !Object.hasOwn(merged, value)) missing.add(value) }
  const group = (id, configured, fields = {}, extra = {}) => ({
    id, configured, status_ok: configured, fields: Object.entries(fields).map(([key, value]) => ({ key, value })), ...extra,
  })
  const snapshots = [{}]
  for (const llm of ['deepseek', 'openai', 'gemini', 'bedrock', 'local', 'hybrid', 'hybrid2', 'hybrid3'])
    for (const configured of [true, false])
      for (const [enabled, mode, scope] of [[false, 'off', 'full_screen'], [true, 'on_demand', 'region'], [true, 'watching', 'selected_window']])
        for (const voice of [true, false])
          for (const caption of ['translated', 'source', 'bilingual'])
            snapshots.push({
              llm_provider: llm, wake_enabled: voice, aec_realtime_enabled: voice, aec_realtime_barge_in: voice,
              wallpaper_caption_mode: caption, chat_translation_subtitles_enabled: voice,
              vision_enabled: enabled, vision_mode: mode, vision_scope: scope,
              model_connections: ['deepseek', 'openai', 'gemini', 'bedrock', 'local', 'hybrid_local'].map(id => group(id, configured)),
              voice_configuration: [
                group('speech_synthesis', configured, { TTS_BACKEND: voice ? 'gpt_sovits' : 'fish_audio' }, { active: true }),
                group('conversation_asr', configured, { ASR_BACKEND: voice ? 'qwen3_asr' : 'sense_voice' }),
                group('wake_asr', configured, { WAKE_ASR_BACKEND: 'sense_voice' }, { active: voice }),
              ],
              model_roles: [
                group('vn_companion', configured, voice ? { VN_LLM_PROVIDER: 'openai', VN_LLM_MODEL: 'gpt-5.1-mini' } : {}),
                group('auip_action', configured),
                group('auip_narration', configured),
              ],
            })
  for (const settings of snapshots)
    for (const provider of ['pi', 'codex', 'openclaw', 'browser'])
      for (const ready of [true, false]) {
        const availability = [{ provider_id: provider, configured: true, ready, registered: ready, reason: ready ? 'ready' : 'provider_unavailable' }]
        for (const capability of buildCapabilityProfiles({ ...settings, work_execution_provider: provider }, availability)) {
          for (const value of [capability.label, capability.category, capability.description, capability.stateLabel]) need(value)
          for (const part of capability.implementation.split(' · ')) need(part)
          for (const scene of capability.scenes) {
            for (const value of [scene.label, scene.detail, scene.stateLabel, scene.actionLabel]) need(value)
            if (scene.configureSection && !scene.actionLabel) need(`Open ${scene.configureSection} settings`)
            for (const value of Object.values(scene.detailVars || {})) need(value)
          }
        }
      }
  assert.deepEqual([...missing].sort(), [], 'Add translations for capability projection text')
})

// Proper names and units that read the same in every locale.
const universalText = new Set(['MiB', 'GitHub', 'Streamable HTTP', 'npx', 'Steam', '0xDC00 Agent'])
// Example input values: URLs, paths, assignments, command flags and environment variable names.
const codeLike = value => /:\/\/|\\|=|^-|^[A-Z0-9_]+$|^\S*\/\S*$/.test(value)

test('console text and accessible names on host elements come from t()', () => {
  const renderer = fileURLToPath(new URL('../src/renderer', import.meta.url))
  const untranslated = []
  const prose = value => /[A-Za-z]{2,}/.test(value) && !universalText.has(value) && !codeLike(value)
  function scan(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, item.name)
      // Work projection surfaces are not localized yet.
      if (item.isDirectory()) { if (item.name !== 'work') scan(filename); continue }
      if (!filename.endsWith('.tsx') || /Work(Preview)?Page\.tsx$/.test(filename)) continue
      const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
      const report = (node, value) => untranslated.push(`${path.relative(renderer, filename)}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1} ${JSON.stringify(value)}`)
      // Custom components translate their own text props and string children.
      const hostElement = tagName => /^[a-z]/.test(tagName.getText(source))
      const callsT = node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't'
        || Boolean(ts.forEachChild(node, child => callsT(child) || undefined))
      function visit(node) {
        if (ts.isJsxText(node) && prose(node.text.trim()) && ts.isJsxElement(node.parent) && hostElement(node.parent.openingElement.tagName)) {
          report(node, node.text.trim())
        }
        if (ts.isJsxAttribute(node) && ['title', 'placeholder', 'aria-label', 'alt'].includes(node.name.getText(source))
          && node.initializer && hostElement(node.parent.parent.tagName)) {
          const value = ts.isJsxExpression(node.initializer) ? node.initializer.expression : node.initializer
          if (value && ts.isStringLiteralLike(value) && prose(value.text)) report(node, value.text)
          if (value && ts.isTemplateExpression(value) && !callsT(value)) report(node, value.getText(source))
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }
  }
  scan(renderer)
  assert.deepEqual(untranslated, [], 'Wrap visible console text in t()')
})

test('the real provider uses both dictionaries and retains English and interpolation behavior', () => {
  for (const locale of ['zh-CN', 'en-US']) {
    const { I18nProvider } = loadTypeScript(new URL('../src/renderer/i18n.tsx', import.meta.url), {
      react: {
        createContext: value => ({ Provider: 'test-provider', value }),
        useState: () => [locale, () => {}], useEffect: () => {},
        useCallback: callback => callback, useMemo: factory => factory(),
        useContext: context => context.value,
      },
    })
    const { t } = I18nProvider({ children: null }).props.value
    assert.equal(t('region'), locale === 'zh-CN' ? '区域' : 'region')
    assert.equal(t('Configure roles'), locale === 'zh-CN' ? '配置职责' : 'Configure roles')
    assert.equal(t('Saving {name}…', { name: '$&' }), locale === 'zh-CN' ? '正在保存 $&…' : 'Saving $&…')
    assert.equal(t('Unknown dynamic diagnostic'), 'Unknown dynamic diagnostic')
  }
})
