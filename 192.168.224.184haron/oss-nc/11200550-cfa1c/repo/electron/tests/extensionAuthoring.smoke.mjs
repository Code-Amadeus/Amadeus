/** Real installer/store lifecycle for four author references. Synthetic confirmation/encryption. */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { app } from 'electron'
import { createHash } from 'node:crypto'
import { DesktopSettingsStore } from '../dist/main/desktopSettings.js'
import { ExtensionInstaller, ExtensionPackageHelper } from '../dist/main/extensions.js'
const root = fileURLToPath(new URL('../../', import.meta.url))
const base = path.resolve(process.env.AMADEUS_AUTHOR_JOURNEY_ROOT)
const relative = path.relative(path.join(root, 'build'), base)
assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative))
const python = process.env.AMADEUS_TEST_PYTHON
assert.ok(python && fs.existsSync(python))
const environment = { ...process.env, PIP_NO_INDEX: '1', PIP_FIND_LINKS: path.join(root, 'vendor/extension-sdk'), PIP_CONFIG_FILE: process.platform === 'win32' ? 'NUL' : '/dev/null' }
const helper = new ExtensionPackageHelper(root, () => python, environment)
const store = new DesktopSettingsStore(path.join(base, 'settings.json'), path.join(base, '.env'), process.platform, helper.validateRecord)
const installer = new ExtensionInstaller(path.join(base, 'extensions'), root, store, helper, () => python, environment, undefined, async () => true)
const definitions = [
  ['tone', 'com.example.tone-tts', 'amadeus_example_tone_tts'],
  ['notes', 'com.example.notes-mcp', 'amadeus_example_notes_mcp'],
  ['handoff', 'com.example.handoff-skill', 'amadeus_example_handoff_skill'],
  ['theme', 'com.example.desktop-theme', 'amadeus_example_theme'],
]
const wheel = (name, version) => path.join(base, 'wheels', `${name}-${version}-py3-none-any.whl`)
const phase = process.argv[2]
app.setPath('userData', path.join(base, 'profile'))
app.whenReady().then(async () => {
try {
await installer.refreshValidation()
if (phase === '--prepare' || phase === '--update') {
  const version = phase === '--prepare' ? '0.1.0' : '0.2.0'
  for (const [name, id, distribution] of definitions) {
    await installer.install(wheel(distribution, version))
    if (phase === '--prepare') await installer.update({ id, enabled: true, ...(['notes', 'handoff'].includes(name) ? { providers: ['pi'] } : {}) })
  }
  const snapshots = JSON.parse(await installer.launchSnapshot())
  for (const [name, id] of definitions) {
    const installed = store.extensionRecords().find(record => record.id === id)
    assert.equal(installed.version, version); assert.equal(installed.enabled, true)
    let record = snapshots.find(record => record.id === id)
    if (name === 'theme') {
      // Pure frontend packages intentionally have no backend startup snapshot.
      assert.equal(record, undefined); assert.equal(installed.sitePackages, '')
      record = { id, version, digest: installed.digest, manifest: installed.manifest, enabled: true,
        package_path: path.join(base, 'extensions', installed.installId, 'package', installed.packageRoot),
        data_path: path.join(base, 'extension-data', createHash('sha256').update(id).digest('hex')), config: {}, secrets: {}, providers: [] }
    }
    assert.ok(!record.startup_error)
    fs.writeFileSync(path.join(base, `${name}-${version}.json`), JSON.stringify(record))
    if (name === 'handoff') fs.writeFileSync(path.join(base, `skill-${version}.json`), JSON.stringify(record))
  }
  console.log(JSON.stringify({ ok: true, phase, packages: definitions.length, nativeDialogs: false }))
} else if (phase === '--finish') {
  await installer.cleanupStoppedBackend()
  for (const [name, id, distribution] of definitions) {
    const record = JSON.parse(fs.readFileSync(path.join(base, `${name}-0.2.0.json`), 'utf8'))
    // The real store owns retained data; the author CLI uses its own disposable data instead.
    const dataPath = record.data_path
    fs.mkdirSync(dataPath, { recursive: true }); fs.writeFileSync(path.join(dataPath, 'retained.txt'), 'synthetic retention')
    await installer.update({ id, enabled: false })
    await installer.remove(id); await installer.cleanupStoppedBackend()
    assert.ok(fs.existsSync(path.join(dataPath, 'retained.txt')))
    await installer.install(wheel(distribution, '0.2.0'))
    const reinstalled = store.extensionRecords().find(record => record.id === id)
    assert.equal(reinstalled.enabled, false); assert.deepEqual(reinstalled.providers, [])
    await installer.remove(id, { deleteData: true }); await installer.cleanupStoppedBackend()
    assert.ok(!fs.existsSync(dataPath))
  }
  assert.deepEqual(store.extensionRecords(), [])
  console.log(JSON.stringify({ ok: true, phase, retainedAndExplicitlyDeleted: true, packages: definitions.length }))
} else throw new Error('Unknown author journey phase')

app.exit(0)
} catch (error) { console.error(error); app.exit(1) }
})
