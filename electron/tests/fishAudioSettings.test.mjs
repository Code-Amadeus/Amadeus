import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
const exports = {}
const secret = 'test-fish-credential'
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: {
    isEncryptionAvailable: () => true,
    encryptString: value => { assert.equal(value, secret); return Buffer.from('test-ciphertext') },
    decryptString: value => { assert.equal(value.toString(), 'test-ciphertext'); return secret },
  } }
  : require(name), exports)

test('Fish WebSocket settings persist; credential reaches backend but not renderer or plaintext store', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-fish-settings-'))
  const file = path.join(root, 'settings.json')
  const dotenv = path.join(root, '.env')
  try {
    const store = new exports.DesktopSettingsStore(file, dotenv)
    const values = {
      TTS_BACKEND: 'fish_audio',
      FISH_TTS_WS_URL: 'wss://api.fish.audio/v1/tts/live',
      FISH_TTS_MODEL: 's2.1-pro-free',
      FISH_TTS_REFERENCE_ID: 'b450b19370434173b121446057622e9b',
      FISH_TTS_LATENCY: 'balanced',
    }
    const snapshot = store.update({}, { values, secrets: { FISH_TTS_API_KEY: secret } })
    assert.equal(snapshot.secrets.FISH_TTS_API_KEY.configured, true)
    assert.equal(JSON.stringify(snapshot).includes(secret), false)
    assert.equal(fs.readFileSync(file, 'utf8').includes(secret), false)
    const environment = new exports.DesktopSettingsStore(file, dotenv).backendEnvironment({})
    for (const [key, value] of Object.entries(values)) assert.equal(environment[key], value)
    assert.equal(environment.FISH_TTS_API_KEY, secret)
    assert.throws(() => store.update({}, { values: { FISH_TTS_WS_URL: 'https://api.fish.audio' } }))
    assert.throws(() => store.update({}, { values: { FISH_TTS_LATENCY: 'invalid' } }))
    assert.throws(() => store.update({}, { values: { FISH_TTS_API_KEY: secret } }))
  } finally {
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()))
    fs.rmSync(root, { recursive: true, force: true })
  }
})
