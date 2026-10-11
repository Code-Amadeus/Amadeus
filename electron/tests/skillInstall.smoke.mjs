/** Real wheel update lifecycle; synthetic storage, no account credentials. */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const root = fileURLToPath(new URL('../../', import.meta.url))
const smokeRoot = path.join(root, 'build', 'skill-desktop-smoke')
fs.mkdirSync(smokeRoot, { recursive: true })
const python = process.env.AMADEUS_TEST_PYTHON
assert.ok(python && fs.existsSync(python))
const phase = process.argv[2] || '--prepare'
const directory = phase === '--prepare' ? fs.mkdtempSync(path.join(smokeRoot, 'case-')) : path.resolve(process.argv[3])
const relative = path.relative(smokeRoot, directory)
assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative))
const mocks = { electron: { safeStorage: { isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(value), decryptString: value => value.toString() } } }
const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), mocks)
const { ExtensionInstaller, ExtensionPackageHelper } = loadTypeScript(new URL('../src/main/extensions.ts', import.meta.url), mocks)
const environment = { ...process.env, PIP_NO_INDEX: '1', PIP_FIND_LINKS: path.join(root, 'build/extension-examples') }
const helper = new ExtensionPackageHelper(root, () => python, environment)
const store = new DesktopSettingsStore(path.join(directory, 'settings.json'), path.join(directory, '.env'), process.platform, helper.validateRecord)
const installer = new ExtensionInstaller(path.join(directory, 'extensions'), root, store, helper, () => python, environment, undefined, async () => true)
const wheel = version => path.join(root, 'build/extension-examples', `amadeus_example_handoff_skill-${version}-py3-none-any.whl`)
const id = 'com.example.handoff-skill'
if (phase === '--prepare') {
  await installer.install(wheel('0.1.0'))
  await assert.rejects(installer.update({ id, enabled: true }), /Select a compatible/)
  await installer.update({ id, enabled: true, providers: ['codex', 'codex_acp', 'pi'] })
  const [first] = JSON.parse(await installer.launchSnapshot())
  fs.writeFileSync(path.join(directory, 'skill-0.1.0.json'), JSON.stringify(first))
  fs.writeFileSync(path.join(first.data_path, 'retained.txt'), 'synthetic package data')
  await installer.install(wheel('0.2.0'))
  const [second] = JSON.parse(await installer.launchSnapshot())
  assert.equal(second.version, '0.2.0')
  assert.equal(second.enabled, true)
  assert.deepEqual(second.providers, first.providers)
  assert.equal(second.data_path, first.data_path)
  assert.notEqual(second.package_path, first.package_path)
  assert.ok(fs.existsSync(first.package_path), 'Running version must survive pending update')
  fs.writeFileSync(path.join(directory, 'skill-0.2.0.json'), JSON.stringify(second))
  console.log(JSON.stringify({ ok: true, phase: 'prepared', directory, oneInstallationIdentity: true, agents: second.providers,
    updateRetainsRunningVersion: true, enableAndBindingsPreserved: true }))
} else {
  const first = JSON.parse(fs.readFileSync(path.join(directory, 'skill-0.1.0.json'), 'utf8'))
  const second = JSON.parse(fs.readFileSync(path.join(directory, 'skill-0.2.0.json'), 'utf8'))
  await installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(first.package_path) && fs.existsSync(second.package_path))
  await installer.remove(id)
  assert.ok(fs.existsSync(second.package_path), 'Removal must wait for stopped backend')
  await installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(second.package_path) && fs.existsSync(path.join(second.data_path, 'retained.txt')))
  assert.deepEqual(JSON.parse(await installer.launchSnapshot()), [])
  await installer.install(wheel('0.2.0'))
  assert.equal(store.extensionRecords()[0].enabled, false)
  assert.deepEqual(store.extensionRecords()[0].providers, [])
  await installer.remove(id, { deleteData: true })
  await installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(second.data_path))
  console.log(JSON.stringify({ ok: true, phase: 'finished', directory, oldVersionCollected: true,
    removedFromStartup: true, retainedAcrossReinstall: true, explicitDataDeletion: true }))
}
