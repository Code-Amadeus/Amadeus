import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
const model = loadTypeScript(new URL('../src/renderer/components/extensionCatalogModel.ts', import.meta.url))
const entry = (version, more = {}) => ({ id: 'org.example.demo', version, sha256: version.repeat(16), size: 100, url: 'https://example.test/pkg.whl', display: { display_name: 'Example', description: 'Tools' }, compatibility: { contributions: [{ kind: 'theme', role: '', contract: 'amadeus.ui.theme/v0' }] }, publisher: {}, review: {}, withdrawal: null, incompatible: [], ...more })
const installed = { id: 'org.example.demo', version: '1.2.0', digest: 'installed', pendingRemoval: false }
test('updates use exact three-integer ordering and only whole compatible, supplied, allowed packages', () => {
  const entries = [entry('1.9.0'), entry('1.10.0'), entry('2.0.0', { incompatible: ['future'] }), entry('3.0.0', { blockedReason: 'blocked' }), entry('4.0.0', { withdrawal: { reason: 'copyright' } })]
  assert.equal(model.recommendedUpdate(entries, installed).version, '1.10.0')
  assert.equal(model.compareCatalogVersions('100000000000000000001.0.0', '100000000000000000000.0.0'), 1)
  for (const version of ['1.0', '1.0.0-beta.1', '1.0.0+build', '01.0.0']) assert.equal(model.recommendedUpdate(entries, { ...installed, version }), undefined)
  assert.equal(model.recommendedUpdate(entries, { ...installed, pendingRemoval: true }), undefined)
  assert.equal(model.recommendedUpdate([entry('1.1.0')], installed), undefined)
})
test('future types stay visible and localized capability search and filtering work', () => {
  const entries = [entry('1.0.0'), entry('1.1.0'), entry('1.0.0', { id: 'future', incompatible: ['unsupported contribution contract'], compatibility: { contributions: [{ kind: 'future', role: '', contract: 'future/v1' }] } })]
  assert.equal(model.catalogGroups(entries, '', '', 'en-US').length, 2)
  assert.equal(model.catalogGroups(entries, '主题', 'theme', 'zh-CN', s => s === 'Theme' ? '主题' : s)[0].versions[0].version, '1.1.0')
  assert.equal(model.catalogGroups(entries, '', 'future', 'en-US')[0].id, 'future')
})
test('same-version different bytes, withdrawal and safety damage cannot look installable', () => {
  assert.match(model.catalogUnavailable(entry('1.2.0'), installed)[0], /different artifact/)
  assert.match(model.catalogUnavailable(entry('1.2.0', { sha256: 'installed' }), installed)[0], /already installed/)
  assert.match(model.catalogUnavailable(entry('2.0.0', { withdrawal: { reason: 'copyright' } }))[0], /copyright/)
  assert.deepEqual(model.catalogUnavailable(entry('2.0.0', { blockedReason: 'damaged' })), ['damaged'])
})
test('catalog text is escaped, has no remote media, and links are action buttons', () => {
  const malicious = '<img src="https://remote.test/image" onerror="alert(1)"> **text**'
  const Component = loadTypeScript(new URL('../src/renderer/components/ExtensionCatalog.tsx', import.meta.url), { '../i18n': { useI18n: () => ({ locale: 'en-US', t: s => s }) }, './FluentIcon': { default: () => null } }).default
  const item = entry('1.0.0', { display: { display_name: malicious, description: malicious, author: { name: malicious }, homepage: 'javascript:alert(1)', repository: 'https://remote.test' } })
  const html = renderToStaticMarkup(React.createElement(Component, { catalog: { entries: [item], cached: false }, installed: [], busy: false, selection: item, onSelect() {}, onInstall() {}, onLink() {} }))
  assert.ok(html.includes('&lt;img'))
  assert.ok(!/<img|<iframe|<script|href=/i.test(html))
  assert.ok(html.includes('Open homepage'))
  assert.equal(model.catalogText({ en: { nested: 'bad' } }, 'en-US', 'fallback'), 'fallback')
})

test('filtering a future version of an otherwise compatible package shows that version and its reasons', () => {
  const current = entry('1.0.0')
  const future = entry('2.0.0', { incompatible: ['unsupported contribution contract'], compatibility: { contributions: [{kind:'future',role:'',contract:'future/v1'}] } })
  const groups = model.catalogGroups([current, future], '', 'future', 'en-US')
  assert.equal(groups[0].versions[0].version, '2.0.0')
  assert.deepEqual(groups[0].versions[0].incompatible, ['unsupported contribution contract'])
})

test('closed and future voice roles are not presented as TTS', () => {
  assert.equal(model.contributionLabel('voice_backend','tts'), 'Speech synthesis')
  assert.equal(model.contributionLabel('voice_backend','asr'), 'Speech recognition')
  assert.equal(model.contributionLabel('voice_backend','future'), 'voice_backend/future')
  const asr = entry('2.0.0', {compatibility:{contributions:[{kind:'voice_backend',role:'asr',contract:'amadeus.voice.asr/v0'}]},incompatible:['unsupported contribution contract']})
  assert.equal(model.catalogApplication(asr), 'Compatibility unavailable')
  assert.equal(model.recommendedUpdate([asr], installed), undefined)
})


test('empty catalog keeps browsing available and explains preview status', () => {
  const Component = loadTypeScript(new URL('../src/renderer/components/ExtensionCatalog.tsx', import.meta.url), { '../i18n': { useI18n: () => ({ locale: 'en-US', t: s => s }) }, './FluentIcon': { default: () => null } }).default
  const html = renderToStaticMarkup(React.createElement(Component, { catalog: { entries: [], cached: false }, installed: [], busy: false, selection: null, onSelect() {}, onInstall() {}, onLink() {} }))
  assert.match(html, /Plugin store preview: no packages have been listed yet/)
  assert.match(html, /Catalog packages/)
})
