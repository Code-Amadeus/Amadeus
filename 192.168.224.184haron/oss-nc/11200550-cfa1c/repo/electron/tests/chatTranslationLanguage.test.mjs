import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { chatTranslationCandidates } from '../src/renderer/components/chatTranslationState.ts'

// Exercise ChatPage's real effects and dependencies, including delayed replies.
const source = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('ChatPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const effects = []
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
    && /setChatTranslationEnabled|chatTranslationGenerationRef/.test(node.arguments[0]?.getText(tree))) {
    effects.push(node.getText(tree))
  }
  ts.forEachChild(node, visit)
}
visit(tree)
const compiled = ts.transpileModule(effects.join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

test('changing the shared language replaces cached Chat subtitles and ignores old pending replies', async () => {
  const messages = [
    { role: 'assistant', text: '一つ目よ。', turnId: 'one' },
    { role: 'assistant', text: '二つ目よ。', turnId: 'two' },
  ]
  const original = structuredClone(messages)
  const requests = [], slots = []
  let notify, translations = {}, index = 0
  const context = vm.createContext({
    connected: true, messages, chatTranslationCandidates,
    chatTranslationEnabled: false, chatTranslationLanguage: 'zh-CN',
    chatTranslationGenerationRef: { current: 0 }, chatTranslationRequestedRef: { current: new Set() },
    setChatTranslationEnabled: value => { context.chatTranslationEnabled = value },
    setChatTranslationLanguage: value => { context.chatTranslationLanguage = value },
    setChatTranslations: value => { translations = typeof value === 'function' ? value(translations) : value },
    subscribe: (_method, callback) => { notify = callback; return () => {} },
    send: (method, params) => {
      if (method === 'system.get_config') return Promise.resolve({
        chat_translation_subtitles_enabled: true, subtitle_translation_language: 'zh-CN',
      })
      assert.equal(method, 'chat.translate')
      return new Promise(resolve => requests.push({ params, resolve }))
    },
    useEffect: (callback, deps) => {
      const slot = index++
      if (slots[slot] && deps.every((value, i) => Object.is(value, slots[slot][i]))) return
      slots[slot] = deps
      callback()
    },
  })
  const render = () => { index = 0; vm.runInContext(compiled, context) }
  render()
  await Promise.resolve()
  render()
  assert.equal(requests.length, 2)
  requests[0].resolve({ translation: '第一句。' })
  await Promise.resolve()
  assert.deepEqual(Object.values(translations), ['第一句。'])

  notify({ values: { chat_translation_subtitles_enabled: true, subtitle_translation_language: 'en-US' } })
  render()
  assert.deepEqual(Object.values(translations), [])
  assert.equal(requests.length, 4)
  requests[1].resolve({ translation: '迟到的旧中文。' })
  await Promise.resolve()
  assert.deepEqual(Object.values(translations), [])
  requests[2].resolve({ translation: 'First line.' })
  requests[3].resolve({ translation: 'Second line.' })
  await Promise.resolve()
  assert.deepEqual(Object.values(translations), ['First line.', 'Second line.'])
  render()
  assert.equal(requests.length, 4, 'ordinary renders reuse current-language subtitles')
  assert.deepEqual(messages, original, 'translations remain display-only')
})
