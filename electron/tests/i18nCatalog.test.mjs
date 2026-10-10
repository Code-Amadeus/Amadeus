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

test('every literal renderer translation call has a translated entry', () => {
  const missing = []
  function scan(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, item.name)
      if (item.isDirectory()) { scan(filename); continue }
      if (!/\.tsx?$/.test(filename)) continue
      const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true)
      function visit(node) {
        if (ts.isCallExpression(node) && node.arguments.length && ts.isStringLiteralLike(node.arguments[0]) && (
          ts.isIdentifier(node.expression) && node.expression.text === 't'
          || ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 't'
        )) {
          const key = node.arguments[0].text
          if (!Object.hasOwn(merged, key)) missing.push(`${filename}: ${key}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }
  }
  scan(fileURLToPath(new URL('../src/renderer', import.meta.url)))
  assert.deepEqual(missing, [], 'Add translations for literal t() calls')
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
