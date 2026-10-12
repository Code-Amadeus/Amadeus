/** Offline real-wheel lifecycle smoke. Only credential encryption is mocked.
 * --prepare leaves a synthetic enabled snapshot for Python/native MCP checks.
 * --finish <prepared-root> verifies retain/reinstall/explicit-delete lifecycle.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const projectRoot = fileURLToPath(new URL('../../', import.meta.url))
const smokeRoot = path.join(projectRoot, 'build', 'extension-desktop-smoke')
fs.mkdirSync(smokeRoot, { recursive: true })
const basePython = process.env.AMADEUS_TEST_PYTHON || path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
assert.ok(fs.existsSync(basePython), 'Set AMADEUS_TEST_PYTHON to the trusted project Python runtime')
const environment = { ...process.env, TEMP: path.join(projectRoot, 'build', 'runtime-temp'), TMP: path.join(projectRoot, 'build', 'runtime-temp'), PIP_NO_INDEX: '1',
  PIP_FIND_LINKS: path.join(projectRoot, 'build', 'extension-examples') }
fs.mkdirSync(environment.TEMP, { recursive: true })
const credentials = { isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(`synthetic-encrypted:${value}`),
  decryptString: value => value.toString().slice('synthetic-encrypted:'.length) }
const mocks = { electron: { safeStorage: credentials } }
const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), mocks)
const { ExtensionInstaller, ExtensionPackageHelper } = loadTypeScript(new URL('../src/main/extensions.ts', import.meta.url), mocks)
const helper = new ExtensionPackageHelper(projectRoot, () => basePython, environment)
const phase = process.argv[2] || '--prepare'
const directory = phase === '--prepare' ? fs.mkdtempSync(path.join(smokeRoot, 'case-')) : path.resolve(process.argv[3] || '')
const relative = path.relative(smokeRoot, directory)
assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative), 'Smoke state must remain under the worktree smoke root')
const store = new DesktopSettingsStore(path.join(directory, 'settings.json'), path.join(projectRoot, '.env'), process.platform, helper.validateRecord)
const installer = new ExtensionInstaller(path.join(directory, 'extensions'), projectRoot, store, helper, () => basePython, environment, undefined, async () => true)
const wheels = ['amadeus_example_mimo_tts-0.1.0-py3-none-any.whl', 'amadeus_example_notes_mcp-0.1.0-py3-none-any.whl',
  'amadeus_example_fish_audio-0.1.0-py3-none-any.whl']
  .map(name => path.join(projectRoot, 'build', 'extension-examples', name))

async function enableAll() {
  for (const record of store.extensionRecords()) {
    const secrets = Object.fromEntries(Object.entries(record.manifest.config || {}).filter(([, field]) => field.secret)
      .map(([key]) => [key, `synthetic-offline-${key}`]))
    await installer.update({ id: record.id, enabled: true, secrets,
      ...(record.manifest.config?.reference_id ? { values: { ...record.values, reference_id: 'synthetic-reference' } } : {}),
      ...(record.manifest.contributes.mcp_server?.length ? { providers: ['codex'] } : {}) })
  }
}

if (phase === '--prepare') {
  for (const wheel of wheels) {
    const settings = await installer.install(wheel)
    assert.ok(settings.extensions.every(item => !item.enabled))
  }
  await enableAll()
  const snapshot = await installer.launchSnapshot()
  const entries = JSON.parse(snapshot)
  assert.equal(entries.length, wheels.length)
  assert.ok(entries.every(item => !item.startup_error))
  assert.ok(entries.every(item => fs.existsSync(item.python_path) && fs.existsSync(item.package_path)))
  const snapshotPath = path.join(directory, 'synthetic-startup-snapshot.json')
  fs.writeFileSync(snapshotPath, snapshot, { mode: 0o600 })
  console.log(JSON.stringify({ ok: true, phase: 'prepared', directory, snapshotPath,
    extensions: entries.map(item => ({ id: item.id, package_path: item.package_path, data_path: item.data_path })) }))
} else if (phase === '--finish') {
  const entries = JSON.parse(await installer.launchSnapshot())
  assert.equal(entries.length, wheels.length)
  for (const entry of entries) {
    fs.writeFileSync(path.join(entry.data_path, 'retention-proof.txt'), 'retained synthetic data')
    await installer.remove(entry.id)
  }
  assert.ok(entries.every(entry => fs.existsSync(entry.package_path)), 'remove request must retain environment until confirmed stop')
  await installer.cleanupStoppedBackend()
  assert.deepEqual(store.extensionRecords(), [])
  assert.ok(entries.every(entry => !fs.existsSync(entry.package_path) && fs.existsSync(path.join(entry.data_path, 'retention-proof.txt'))))
  for (const wheel of wheels) await installer.install(wheel)
  assert.ok(store.snapshot({}).extensions.every(item => !item.enabled && Object.values(item.secretConfigured).every(configured => !configured)))
  await enableAll()
  const reinstalled = JSON.parse(await installer.launchSnapshot())
  assert.ok(reinstalled.every(entry => entries.find(old => old.id === entry.id).data_path === entry.data_path))
  for (const entry of reinstalled) await installer.remove(entry.id, { deleteData: true })
  await installer.cleanupStoppedBackend()
  assert.deepEqual(store.extensionRecords(), [])
  assert.ok(reinstalled.every(entry => !fs.existsSync(entry.data_path) && !fs.existsSync(entry.package_path)))
  console.log(JSON.stringify({ ok: true, phase: 'finished', directory, retainedAcrossReinstall: true, explicitDataDeletion: true }))
} else throw new Error('Expected --prepare or --finish')
