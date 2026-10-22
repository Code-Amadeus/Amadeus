import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const profiles = loadTypeScript(new URL('../src/renderer/components/chatModelCapabilities.ts', import.meta.url))
const catalog = ['deepseek', 'openai', 'gemini', 'bedrock', 'local', 'hybrid', 'hybrid2', 'hybrid3']
const configured = ids => ids.map(id => ({ id, configured: true }))

test('the model menu lists configured profiles by raw ID and always keeps the current one', () => {
  assert.deepEqual(profiles.configuredChatProfiles(catalog, null, 'deepseek'), catalog, 'without connection facts every profile stays listed')
  assert.deepEqual(profiles.configuredChatProfiles(catalog, configured(['deepseek']), 'deepseek'), ['deepseek'])
  assert.deepEqual(profiles.configuredChatProfiles(catalog, configured(['deepseek', 'hybrid_local']), 'deepseek'), ['deepseek', 'hybrid2'],
    'a hybrid profile needs the local head and its remote model')
  assert.deepEqual(profiles.configuredChatProfiles(catalog, configured(['openai']), 'gemini'), ['openai', 'gemini'], 'an unconfigured current profile stays')
  assert.deepEqual(profiles.configuredChatProfiles(catalog, configured(['openai']), 'custom'), ['openai', 'custom'])
  assert.deepEqual(profiles.configuredChatProfiles(catalog, [{ id: 'deepseek', configured: false, status_ok: true }], 'openai'), ['openai'],
    'configured decides, not reachable')
})

test('every main-chat profile maps to the connection groups it needs', () => {
  assert.deepEqual(catalog.map(profile => profiles.chatProfileConnections(profile)), [
    ['deepseek'], ['openai'], ['gemini'], ['bedrock'], ['local'],
    ['hybrid_local', 'bedrock'], ['hybrid_local', 'deepseek'], ['hybrid_local', 'openai'],
  ])
})

// Layout contracts of the production ChatPage JSX.
const source = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('ChatPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const opening = node => ts.isJsxElement(node) ? node.openingElement : ts.isJsxSelfClosingElement(node) ? node : null
const attribute = (node, name) => {
  const found = opening(node)?.attributes.properties.find(item => ts.isJsxAttribute(item) && item.name.getText(tree) === name)
  if (!found?.initializer) return undefined
  return ts.isStringLiteral(found.initializer) ? found.initializer.text : found.initializer.expression.getText(tree)
}
function elements(root, predicate) {
  const found = []
  const visit = node => { if (opening(node) && predicate(node)) found.push(node); ts.forEachChild(node, visit) }
  visit(root)
  return found
}
const byClass = (root, className) => elements(root, node => attribute(node, 'className') === className)

test('the chat header owns the render toggle: pressed while requested, disabled offline', () => {
  const [bar] = byClass(tree, 'chat-bar')
  assert.ok(bar, 'chat header')
  const [toggle] = elements(bar, node => opening(node).tagName.getText(tree) === 'SettingsButton' && attribute(node, 'aria-pressed') !== undefined)
  assert.ok(toggle, 'render toggle in the chat header')
  assert.equal(attribute(toggle, 'aria-pressed'), 'renderRequested')
  assert.equal(attribute(toggle, 'onClick'), 'onToggleRender')
  assert.equal(attribute(toggle, 'disabled'), '!connected')
})

test('a pending approval is pinned above the composer instead of scrolling with the messages', () => {
  const [messages] = byClass(tree, 'chat-message-list')
  assert.deepEqual(elements(messages, node => attribute(node, 'role') === 'dialog'), [], 'no approval inside the message list')
  const [composerBar] = byClass(tree, 'chat-composer-bar')
  const [approval] = elements(composerBar, node => attribute(node, 'role') === 'dialog')
  const [composer] = byClass(composerBar, 'chat-composer')
  assert.ok(approval && composer)
  assert.ok(approval.getStart() < composer.getStart(), 'the approval sits above the composer')
  assert.match(approval.getText(tree), /attentionRequests\.length > 1/, 'more waiting requests are counted')
})
