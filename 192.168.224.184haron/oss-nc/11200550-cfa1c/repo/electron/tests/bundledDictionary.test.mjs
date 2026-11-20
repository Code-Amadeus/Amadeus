import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { bundledDictionaryEnvironment } from '../src/main/bundledDictionary.ts'
const require = createRequire(import.meta.url)
const { prepareDictionary, verifyDictionary } = require('../scripts/build-runtime-resources.cjs')
const pin = JSON.parse(fs.readFileSync(new URL('../../release/japanese_dictionary.json', import.meta.url), 'utf8'))

test('packaged and source launches supply the actual immutable dictionary location', () => {
  assert.deepEqual(bundledDictionaryEnvironment('/backend', '/app/resources', true),
    { AMADEUS_BUNDLED_JAPANESE_DICTIONARY: path.join('/app/resources/assets/bundled/japanese-pronunciation') })
  assert.deepEqual(bundledDictionaryEnvironment('/backend', '/app/resources', false),
    { AMADEUS_BUNDLED_JAPANESE_DICTIONARY: path.join('/backend/assets/bundled/japanese-pronunciation') })
})

test('application packaging rejects a missing or tampered default, and never includes personal files', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-bundled-'))
  try {
    fs.mkdirSync(path.join(root, 'release'))
    const fixture = structuredClone(pin)
    const source = path.join(root, 'assets/bundled/japanese-pronunciation')
    assert.throws(() => verifyDictionary(source, fixture), /missing/)
    fs.mkdirSync(source, { recursive: true })
    for (const record of fixture.files) {
      const data = Buffer.from(path.basename(record.path))
      record.size = data.length
      record.sha256 = createHash('sha256').update(data).digest('hex')
      fs.writeFileSync(path.join(source, path.basename(record.path)), data)
    }
    fs.writeFileSync(path.join(root, 'release/japanese_dictionary.json'), JSON.stringify(fixture))
    const staged = prepareDictionary(root)
    assert.equal(fs.readdirSync(staged).length, 9)
    verifyDictionary(staged, fixture)
    fs.writeFileSync(path.join(source, 'user.csv'), 'personal')
    assert.throws(() => prepareDictionary(root), /missing|unexpected/)
    fs.unlinkSync(path.join(source, 'user.csv'))
    fs.appendFileSync(path.join(source, 'userdict.csv'), 'changed')
    assert.throws(() => prepareDictionary(root), /differs/)
    fixture.files[0].path = 'assets/text/ja/user.csv'
    assert.throws(() => verifyDictionary(staged, fixture), /personal/)
  } finally { fs.rmSync(root, { recursive: true, force: true }) }
})

test('only the explicitly named developer config omits dictionary resources', () => {
  const app = require('../package.json').build
  const developer = require('../electron-builder-code-only.cjs')
  assert.equal(app.beforePack, 'scripts/build-runtime-resources.cjs')
  assert.ok(app.extraResources.some(r => r.to === 'assets/bundled/japanese-pronunciation'))
  assert.ok(!developer.extraResources.some(r => r.to === 'assets/bundled/japanese-pronunciation'))
  assert.match(developer.artifactName, /code-only/)
})
