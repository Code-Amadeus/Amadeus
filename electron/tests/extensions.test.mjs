import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { createHash } from 'node:crypto'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const tempRoot = fileURLToPath(new URL('../../build/extension-desktop-tests/', import.meta.url))
fs.mkdirSync(tempRoot, { recursive: true })
const credentials = { isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(`protected:${value}`),
  decryptString: value => { const text = value.toString(); if (!text.startsWith('protected:')) throw new Error('bad credential'); return text.slice(10) } }
const mocks = { electron: { safeStorage: credentials } }
const { DesktopSettingsStore, EXTENSIONS_ENV } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), mocks)
const { ExtensionInstaller, ExtensionPackageHelper, ExtensionPreparationShutdownError, assertExtensionPath, removeExtensionDirectory,
  extensionInstallerEnvironment, runExtensionCommand } = loadTypeScript(new URL('../src/main/extensions.ts', import.meta.url), mocks)

function manifest(kind = 'voice_backend') {
  return { schema: 'amadeus.extension/v0', id: 'org.example.demo', version: '0.1.0', display_name: 'Demo', description: 'Test package',
    author: { name: 'Example' }, engines: { amadeus_host_api: '>=0.1,<0.2' },
    runtime: { kind: 'python', entry: 'demo.extension:extension', requires_python: '>=3.10' },
    contributes: kind === 'voice_backend' ? { voice_backend: [{ id: 'voice', contract: 'amadeus.voice.tts/v0', role: 'tts', deployment: 'remote', languages: ['en'], streaming: true }] }
      : { mcp_server: [{ id: 'tools', contract: 'amadeus.mcp-server/v0', transport: 'http', url: '${config.endpoint}' }] },
    config: { endpoint: { type: 'url', title: 'Endpoint', default: 'https://example.invalid/api' },
      token: { type: 'string', title: 'Token', secret: true, required: true },
      streaming: { type: 'boolean', title: 'Streaming', default: true } } }
}

function fixture(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(tempRoot, 'case-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const file = path.join(directory, 'settings.json')
  const selectedWheel = path.join(directory, 'demo-0.1.0-py3-none-any.whl')
  fs.writeFileSync(selectedWheel, 'synthetic wheel bytes')
  const sdk = path.join(directory, 'vendor', 'extension-sdk', 'amadeus_extension_sdk-0.1.0-py3-none-any.whl')
  fs.mkdirSync(path.dirname(sdk), { recursive: true })
  fs.writeFileSync(sdk, 'trusted SDK')
  const provenance = path.join(directory, 'server', 'extensions', 'protocol-source.json')
  fs.mkdirSync(path.dirname(provenance), { recursive: true })
  fs.writeFileSync(provenance, JSON.stringify({ sdk: { version: '0.1.0', filename: path.basename(sdk),
    sha256: createHash('sha256').update('trusted SDK').digest('hex') } }))
  const metadata = options.manifest || manifest()
  const sitePackages = process.platform === 'win32' ? ['Lib', 'site-packages'] : ['lib', 'python3.12', 'site-packages']
  const pythonParts = process.platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python']
  const store = new DesktopSettingsStore(file, path.join(directory, '.env'), process.platform, (value, config) => {
    if (options.validatorState?.unavailable) return 'unavailable'
    if (options.validatorState?.incompatible) return false
    return value.id === metadata.id && !value.permissions?.host?.length && !Object.keys(config).some(key => !value.config?.[key] || value.config[key].secret)
  })
  const calls = []
  const helper = { refreshRecords: async () => {}, call: async (command, args = [], input) => {
    calls.push({ command, args, input })
    if (command === 'inspect-wheel') {
      if (options.inspectionError) throw new Error(options.inspectionError)
      assert.notEqual(args[0], selectedWheel)
      return { ok: true, manifest: metadata, digest: createHash('sha256').update(fs.readFileSync(args[0])).digest('hex'),
        package_root: 'demo', manifest_member: 'demo/amadeus-extension.json', distribution: 'demo' }
    }
    if (command === 'validate-installed') {
      assert.ok(fs.existsSync(path.join(args[0], 'amadeus-extension.json')))
      return { ok: true }
    }
    if (command === 'prepare-data-wheel') {
      const packageDir = path.join(args[2], 'demo')
      fs.mkdirSync(packageDir, { recursive: true })
      fs.writeFileSync(path.join(packageDir, 'amadeus-extension.json'), JSON.stringify(metadata))
      fs.writeFileSync(path.join(packageDir, '__init__.py'), 'raise Exception("not an import")')
      return { ok: true }
    }
    const { config, allow_incomplete } = input
    if (Object.keys(config).some(key => !metadata.config[key])) throw new Error('configuration contains undeclared fields')
    if (!allow_incomplete && !config.token) throw new Error('required configuration is missing: token')
    if (config.streaming !== undefined && typeof config.streaming !== 'boolean') throw new Error('invalid configuration type for streaming')
    return { ok: true, config: { endpoint: 'https://example.invalid/api', streaming: true, ...Object.fromEntries(Object.entries(config).filter(([key]) => !metadata.config[key].secret)) } }
  } }
  const commandCalls = []
  const run = async (command, args, invocation) => {
    commandCalls.push({ command, args, invocation })
    if (options.unconfirmedShutdown) throw new ExtensionPreparationShutdownError()
    if (options.failPreparation) throw new Error('simulated preparation failure')
    if (args.includes('venv')) {
      const venv = args.at(-1)
      fs.mkdirSync(path.join(venv, ...sitePackages), { recursive: true })
      fs.mkdirSync(path.join(venv, pythonParts[0]), { recursive: true })
      fs.writeFileSync(path.join(venv, ...pythonParts), '')
      return ''
    }
    if (args.includes('-c')) return args.at(-1).includes('configuration') ? JSON.stringify(options.pipConfiguration || {})
      : path.join(path.dirname(path.dirname(command)), ...sitePackages)
    if (args.at(-1).endsWith('demo-0.1.0-py3-none-any.whl')) {
      const packageDir = path.join(path.dirname(path.dirname(command)), ...sitePackages, 'demo')
      fs.mkdirSync(packageDir)
      fs.writeFileSync(path.join(packageDir, 'amadeus-extension.json'), JSON.stringify(metadata))
      fs.writeFileSync(path.join(packageDir, 'extension.py'), 'raise Exception("package code must not execute during preparation")')
    }
    return ''
  }
  const root = path.join(directory, 'extensions')
  const environment = options.environment || {}
  const sdkDirectory = options.packagedSdk ? path.join(directory, 'resources', 'extension-sdk') : undefined
  if (sdkDirectory) {
    fs.mkdirSync(sdkDirectory, { recursive: true })
    const name = 'amadeus_extension_sdk-0.1.0-py3-none-any.whl'
    fs.renameSync(path.join(directory, 'vendor', 'extension-sdk', name), path.join(sdkDirectory, name))
  }
  const installer = new ExtensionInstaller(root, directory, store, helper, () => 'trusted-python', environment, run,
    async review => { calls.push({ command: 'confirm', review }); return options.confirm !== false }, sdkDirectory)
  return { directory, root, file, store, installer, selectedWheel, calls, commandCalls, metadata }
}

test('packaged installer uses the shipped SDK without a source build directory', async t => {
  const f = fixture(t, { packagedSdk: true })
  await f.installer.install(f.selectedWheel)
  const shipped = path.join(f.directory, 'resources', 'extension-sdk', 'amadeus_extension_sdk-0.1.0-py3-none-any.whl')
  const stagedSdk = f.commandCalls.find(call => call.args.at(-1).endsWith(path.basename(shipped)))?.args.at(-1)
  assert.ok(stagedSdk && stagedSdk !== shipped)
  assert.deepEqual(fs.readFileSync(stagedSdk), fs.readFileSync(shipped))
  assert.equal(f.store.extensionRecords().length, 1)
})

test('no-code installation uses inspected resource preparation without Python environment or pip', async t => {
  const metadata = manifest('mcp_server')
  metadata.runtime = { kind: 'none' }
  const f = fixture(t, { manifest: metadata })
  fs.unlinkSync(path.join(f.directory, 'vendor/extension-sdk/amadeus_extension_sdk-0.1.0-py3-none-any.whl'))
  await f.installer.install(f.selectedWheel)
  assert.equal(f.store.extensionRecords()[0].sitePackages, '')
  assert.deepEqual(f.commandCalls, [])
  assert.equal(f.calls.filter(call => call.command === 'prepare-data-wheel').length, 1)
  await f.installer.update({ id: metadata.id, enabled: true, providers: ['pi'], secrets: { token: 'synthetic' } })
  const [startup] = JSON.parse(await f.installer.launchSnapshot())
  assert.equal(startup.python_path, '')
  assert.ok(startup.package_path.includes(`${path.sep}package${path.sep}demo`))
})

test('package revisions change only with committed content; backend receipt owns applied revision', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  assert.equal(f.store.extensionRecords()[0].revision, 1)
  const change = { id: f.metadata.id, enabled: true, secrets: { token: 'synthetic' } }
  await f.installer.update(change)
  assert.equal(f.store.extensionRecords()[0].revision, 2)
  const pending = f.store.pendingRevisionSnapshot()
  await f.installer.update(change)
  assert.equal(f.store.extensionRecords()[0].revision, 2)
  assert.deepEqual(f.store.pendingRevisionSnapshot(), pending)
  const launch = await f.installer.prepareLaunch()
  assert.equal(await f.installer.acceptLaunch(launch, {}), false)
  assert.equal(f.store.snapshot({}).extensions[0].appliedRevision, undefined)
  assert.equal(await f.installer.acceptLaunch(launch, { [f.metadata.id]: 2 }), true)
  f.store.markApplied({}, launch.pendingRevisions)
  assert.equal(f.store.snapshot({}).extensions[0].appliedRevision, 2)
  await f.installer.update({ id: f.metadata.id, values: { streaming: false } })
  const status = f.store.snapshot({}).extensions[0]
  assert.equal(status.revision, 3)
  assert.equal(status.appliedRevision, 2)
  assert.equal(status.restartRequired, true)
})

test('legacy record revision is interpreted without rewriting or losing its installation', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const persisted = JSON.parse(fs.readFileSync(f.file, 'utf8'))
  delete persisted.extensions[f.metadata.id].revision
  fs.writeFileSync(f.file, JSON.stringify(persisted))
  const bytes = fs.readFileSync(f.file, 'utf8')
  assert.equal(f.store.snapshot({}).extensions[0].revision, 1)
  assert.equal(fs.readFileSync(f.file, 'utf8'), bytes)
  assert.equal(f.store.extensionRecords()[0].installId, persisted.extensions[f.metadata.id].installId)
})

test('existing no-code packages in private environments retain their original layout', async t => {
  const f = fixture(t, { manifest: manifest('mcp_server') })
  await f.installer.install(f.selectedWheel)
  f.metadata.runtime = { kind: 'none' }
  const [legacy] = f.store.storedExtensionRecords()
  legacy.manifest.runtime = { kind: 'none' }
  f.store.saveExtension({}, legacy)
  await f.installer.update({ id: f.metadata.id, enabled: true, providers: ['pi'], secrets: { token: 'synthetic' } })
  const [startup] = JSON.parse(await f.installer.launchSnapshot())
  assert.ok(startup.package_path.includes(`${path.sep}venv${path.sep}`))
  assert.ok(startup.python_path)
})

test('version update preserves enabled bindings and credentials without removing running code', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, providers: ['codex', 'pi'], secrets: { token: 'own-key' } })
  const [before] = f.store.extensionRecords()
  f.metadata.version = '0.2.0'
  fs.writeFileSync(f.selectedWheel, 'different wheel bytes')
  await f.installer.install(f.selectedWheel)
  const [after] = f.store.extensionRecords()
  assert.equal(after.version, '0.2.0')
  assert.equal(after.enabled, true)
  assert.deepEqual(after.providers, ['codex', 'pi'])
  assert.deepEqual(after.encryptedSecrets, before.encryptedSecrets)
  assert.notEqual(after.installId, before.installId)
  assert.ok(fs.existsSync(path.join(f.root, before.installId)))
  await f.installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(path.join(f.root, before.installId)))
  assert.ok(fs.existsSync(path.join(f.root, after.installId)))
})

test('failed update leaves the installed version and saved intent unchanged', async t => {
  const options = {}
  const f = fixture(t, options)
  await f.installer.install(f.selectedWheel)
  const before = f.store.extensionRecords()
  f.metadata.version = '0.2.0'
  options.failPreparation = true
  await assert.rejects(f.installer.install(f.selectedWheel), /preparation failure/)
  assert.deepEqual(f.store.extensionRecords(), before)
  assert.deepEqual(fs.readdirSync(f.root), [before[0].installId])
})

test('install, expanded update and enable require disclosure confirmation before execution or persistence', async t => {
  const options = { confirm: false }
  const f = fixture(t, options)
  await assert.rejects(f.installer.install(f.selectedWheel), /cancelled/)
  assert.deepEqual(f.commandCalls, [])
  assert.deepEqual(f.store.storedExtensionRecords(), [])
  options.confirm = true
  await f.installer.install(f.selectedWheel)
  const before = f.store.storedExtensionRecords()
  options.confirm = false
  await assert.rejects(f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'never saved' } }), /cancelled/)
  assert.deepEqual(f.store.storedExtensionRecords(), before)
  const commands = f.commandCalls.length
  f.metadata.version = '0.2.0'
  f.metadata.permissions = { os: { network: ['new.example.invalid'], process: ['python'] } }
  await assert.rejects(f.installer.install(f.selectedWheel), /cancelled/)
  assert.equal(f.commandCalls.length, commands)
  assert.deepEqual(f.store.storedExtensionRecords(), before)
  const review = f.calls.filter(call => call.command === 'confirm').at(-1).review
  assert.equal(review.operation, 'update')
  assert.equal(review.previousVersion, '0.1.0')
  assert.deepEqual(review.manifest.permissions.os.network, ['new.example.invalid'])
})

test('ownership marker is restored if removing the empty installation directory fails', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const record = f.store.extensionRecords()[0]
  const directory = path.join(f.root, record.installId)
  const remove = fs.rmdirSync
  let locked = true
  t.mock.method(fs, 'rmdirSync', target => {
    if (target === directory && locked) throw Object.assign(new Error('busy'), { code: 'EBUSY' })
    return remove(target)
  })
  assert.throws(() => removeExtensionDirectory(f.root, directory, record.installId), /busy/)
  assert.equal(JSON.parse(fs.readFileSync(path.join(directory, '.amadeus-extension-install.json'), 'utf8')).installId, record.installId)
  locked = false
  removeExtensionDirectory(f.root, directory, record.installId)
  assert.ok(!fs.existsSync(directory))
})

test('wheel install prepares an independent environment, validates, saves disabled intent and pending restart', async t => {
  const f = fixture(t)
  const settings = await f.installer.install(f.selectedWheel)
  assert.equal(settings.extensions.length, 1)
  assert.equal(settings.extensions[0].enabled, false)
  assert.equal(settings.extensions[0].status, 'disabled')
  assert.deepEqual(settings.extensions[0].secretConfigured, { token: false })
  assert.ok(settings.pendingRevisions[EXTENSIONS_ENV])
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  assert.ok(f.commandCalls[0].args.includes('--copies'))
  const pip = f.commandCalls.filter(call => call.args.includes('pip'))
  assert.equal(pip.length, 2)
  assert.ok(pip[0].args.includes('--no-deps'))
  assert.ok(pip.every(call => call.args.includes('--only-binary=:all:')))
  assert.ok(pip.every(call => call.args.includes('--target') && call.args.includes('--no-user')))
  assert.ok(pip[1].args.includes('--constraint'))
  assert.equal(f.calls.at(-2).command, 'validate-installed')
  assert.equal(f.calls.at(-1).command, 'validate-config')
  assert.ok(!f.commandCalls.some(call => call.args.includes('demo.extension:extension')))
})

test('configuration validates before enable; encrypted credentials never enter the renderer or generic backend environment', async t => {
  const f = fixture(t, { environment: { DEEPSEEK_API_KEY: 'unrelated-host-secret', AMADEUS_EXTENSIONS: 'injected-authority' } })
  await f.installer.install(f.selectedWheel)
  await assert.rejects(f.installer.update({ id: f.metadata.id, enabled: true }), /required configuration/)
  assert.equal(f.store.extensionRecords()[0].enabled, false)
  const settings = await f.installer.update({ id: f.metadata.id, enabled: true, values: { streaming: false }, secrets: { token: 'extension-private-secret' } })
  assert.equal(settings.extensions[0].values.streaming, false)
  assert.equal(settings.extensions[0].secretConfigured.token, true)
  assert.ok(!JSON.stringify(settings).includes('extension-private-secret'))
  assert.ok(!fs.readFileSync(f.file, 'utf8').includes('extension-private-secret'))
  assert.equal(f.store.backendEnvironment({})[EXTENSIONS_ENV], undefined)
  const launch = JSON.parse(await f.installer.launchSnapshot())
  assert.equal(launch[0].secrets.token, 'extension-private-secret')
  assert.equal(launch[0].config.streaming, false)
  assert.ok(!JSON.stringify(launch).includes('unrelated-host-secret'))
  assert.ok(settings.extensionErrors.some(message => message.includes('ignored')))
  assert.ok(f.commandCalls.every(call => call.invocation.env.DEEPSEEK_API_KEY === undefined && call.invocation.env.AMADEUS_EXTENSIONS === undefined))
})

test('optional public config null resets defaults and required secret cannot be cleared while enabled', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, values: { streaming: false }, secrets: { token: 'secret' } })
  const reset = await f.installer.update({ id: f.metadata.id, values: { streaming: null } })
  assert.equal(reset.extensions[0].values.streaming, true)
  await assert.rejects(f.installer.update({ id: f.metadata.id, secrets: { token: null } }), /required configuration/)
  assert.equal(f.store.extensionSecrets(f.store.extensionRecords()[0]).token, 'secret')
  const disabled = await f.installer.update({ id: f.metadata.id, enabled: false, secrets: { token: null } })
  assert.equal(disabled.extensions[0].secretConfigured.token, false)
  await assert.rejects(f.installer.update({ id: f.metadata.id, values: { token: 'plaintext' } }), /credential field/)
  await assert.rejects(f.installer.update({ id: f.metadata.id, values: { streaming: 'yes' } }), /configuration type/)
})

test('declarative MCP packages require an explicit Work Provider binding to enable', async t => {
  const f = fixture(t, { manifest: manifest('mcp_server') })
  await f.installer.install(f.selectedWheel)
  await assert.rejects(f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } }), /Work Provider/)
  const saved = await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' }, providers: ['pi', 'pi'] })
  assert.deepEqual(saved.extensions[0].providers, ['pi'])
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot())[0].providers, ['pi'])
})

test('only TTS selection accepts installed qualified contribution IDs and parent locks still apply', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const id = `${f.metadata.id}/voice`
  const saved = f.store.update({}, { values: { TTS_BACKEND: id } })
  assert.equal(saved.values.TTS_BACKEND, id)
  await assert.rejects(f.installer.update({ id: f.metadata.id, enabled: true, providers: ['../escape'] }), /Provider binding/)
  assert.throws(() => f.store.update({}, { values: { ASR_BACKEND: id } }), /Invalid backend identifier/)
  assert.throws(() => f.store.update({}, { values: { TTS_BACKEND: `${f.metadata.id}/missing` } }), /Invalid backend identifier/)
  assert.throws(() => f.store.update({ TTS_BACKEND: 'mimo' }, { values: { TTS_BACKEND: id } }), /locked/)
})

test('uninstall persists disabled pending removal and deletes files only at the stopped-backend boundary', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } })
  f.store.update({}, { values: { TTS_BACKEND: `${f.metadata.id}/voice` } })
  const record = f.store.extensionRecords()[0]
  const directory = path.join(f.root, record.installId)
  const requested = await f.installer.remove(record.id)
  assert.ok(fs.existsSync(directory))
  assert.equal(requested.extensions[0].pendingRemoval, true)
  assert.equal(requested.values.TTS_BACKEND, `${f.metadata.id}/voice`)
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  await assert.rejects(f.installer.update({ id: record.id, enabled: true }), /pending removal/)
  await f.installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(directory))
  assert.deepEqual(f.store.extensionRecords(), [])
  assert.ok(f.store.snapshot({}).pendingRevisions[EXTENSIONS_ENV])
  f.store.markApplied({}, f.store.pendingRevisionSnapshot())
  assert.equal(f.store.snapshot({}).restartRequired, false)
})

test('old startup acknowledgments cannot clear a newer extension mutation', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const launched = await f.installer.prepareLaunch()
  await f.installer.update({ id: f.metadata.id, values: { streaming: false } })
  f.store.markApplied({}, launched.pendingRevisions)
  assert.ok(f.store.snapshot({}).pendingRevisions[EXTENSIONS_ENV] > launched.pendingRevisions[EXTENSIONS_ENV])
})

test('failed preparation removes verified staging files and never creates an installed record', async t => {
  const f = fixture(t, { failPreparation: true })
  await assert.rejects(f.installer.install(f.selectedWheel), /simulated preparation/)
  assert.deepEqual(f.store.extensionRecords(), [])
  assert.deepEqual(fs.readdirSync(f.root), [])
})

test('pip installation path overrides are rejected before installation while transport configuration is accepted', async t => {
  const unsafe = fixture(t, { pipConfiguration: { 'install.root': '../outside', 'global.index-url': 'https://user:password@example.invalid/simple' } })
  await assert.rejects(unsafe.installer.install(unsafe.selectedWheel), /installation path overrides/)
  assert.ok(!unsafe.commandCalls.some(call => call.args.includes('pip')))
  assert.deepEqual(unsafe.store.extensionRecords(), [])
  assert.deepEqual(fs.readdirSync(unsafe.root), [])
  const transport = fixture(t, { pipConfiguration: { 'global.index-url': 'https://user:password@example.invalid/simple', 'global.proxy': 'https://proxy.invalid' } })
  const saved = await transport.installer.install(transport.selectedWheel)
  assert.equal(saved.extensions.length, 1)
  assert.ok(!JSON.stringify(saved).includes('password'))
})

test('unconfirmed preparation shutdown retains staging without enabling or persisting a package', async t => {
  const f = fixture(t, { unconfirmedShutdown: true })
  await assert.rejects(f.installer.install(f.selectedWheel), /shutdown could not be confirmed/)
  assert.deepEqual(f.store.extensionRecords(), [])
  assert.equal(fs.readdirSync(f.root).length, 1)
  assert.ok(f.store.snapshot({}).extensionErrors.some(message => message.includes('staging files were retained')))
  await f.installer.cleanupStoppedBackend()
  assert.equal(fs.readdirSync(f.root).length, 1)
})

test('overflowed preparation stops the real owned Windows process tree before reporting failure', { skip: process.platform !== 'win32' }, async t => {
  const directory = fs.mkdtempSync(path.join(tempRoot, 'process-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const source = 'const {spawn}=require("node:child_process"); const fs=require("node:fs"); const child=spawn(process.execPath,["-e","setInterval(()=>{},1000)"],{stdio:"ignore"}); fs.writeFileSync("descendant.json",JSON.stringify({pid:process.pid,childPid:child.pid})); process.stdout.write("x".repeat(1300000)); setInterval(()=>{},1000);'
  await assert.rejects(runExtensionCommand(process.execPath, ['-e', source], { cwd: directory, env: extensionInstallerEnvironment(process.env) }), /too much output/)
  const identities = JSON.parse(fs.readFileSync(path.join(directory, 'descendant.json'), 'utf8'))
  for (const pid of [identities.pid, identities.childPid]) assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' })
})

test('primary-write failure retains the environment referenced by the durable backup and normal recovery can use it', async t => {
  const f = fixture(t)
  f.store.update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' } })
  const nativeRename = fs.renameSync
  fs.renameSync = (source, target) => {
    if (target === f.file && source === `${f.file}.tmp`) throw Object.assign(new Error('primary write unavailable'), { code: 'EACCES' })
    return nativeRename(source, target)
  }
  try { await assert.rejects(f.installer.install(f.selectedWheel), /primary write unavailable/) }
  finally { fs.renameSync = nativeRename }
  assert.deepEqual(f.store.extensionRecords(), [])
  const backup = JSON.parse(fs.readFileSync(`${f.file}.bak`, 'utf8'))
  const record = backup.extensions[f.metadata.id]
  assert.ok(record)
  assert.ok(fs.existsSync(path.join(f.root, record.installId)))
  await f.installer.cleanupStoppedBackend()
  assert.ok(fs.existsSync(path.join(f.root, record.installId)), 'valid backup reference retains environment')
  fs.writeFileSync(f.file, 'damaged primary')
  assert.equal(f.store.snapshot({}).extensions[0].id, f.metadata.id)
  f.store.markApplied({}, {})
  await f.installer.remove(f.metadata.id)
  await f.installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(path.join(f.root, record.installId)))
  assert.equal(f.store.extensionInstallationReferences().size, 0)
})

test('missing records never rediscover or enable a package; orphan cleanup only removes owned directories', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const record = f.store.extensionRecords()[0]
  fs.rmSync(f.file)
  fs.rmSync(`${f.file}.bak`)
  assert.deepEqual(f.store.snapshot({}).extensions, [])
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  const unrelated = path.join(f.root, 'unrelated')
  fs.mkdirSync(unrelated)
  fs.writeFileSync(path.join(unrelated, 'keep'), 'not installation-owned')
  const unverified = path.join(f.root, 'a'.repeat(32))
  fs.mkdirSync(unverified)
  fs.writeFileSync(path.join(unverified, 'keep'), 'no ownership marker')
  await f.installer.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(path.join(f.root, record.installId)))
  assert.ok(fs.existsSync(path.join(unrelated, 'keep')))
  assert.ok(fs.existsSync(path.join(unverified, 'keep')))
  assert.ok(f.store.snapshot({}).extensionErrors.some(message => message.includes('ownership')))
})

test('invalid durable extension records enable nothing and display validation failure', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const parsed = JSON.parse(fs.readFileSync(f.file, 'utf8'))
  parsed.extensions[f.metadata.id].enabled = true
  parsed.extensions[f.metadata.id].installId = '../../outside'
  fs.writeFileSync(f.file, JSON.stringify(parsed))
  const state = f.store.snapshot({})
  assert.deepEqual(state.extensions, [])
  assert.ok(state.extensionErrors.length)
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  assert.throws(() => f.store.requestExtensionRemoval({}, '__proto__'), /not found/)
})

test('temporary validator failure preserves original records and credentials across unrelated saves, then recovers automatically', async t => {
  const validatorState = { unavailable: false }
  const f = fixture(t, { validatorState })
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, values: { streaming: false }, secrets: { token: 'preserved secret' } })
  f.store.markApplied({}, f.store.pendingRevisionSnapshot())
  const before = JSON.parse(fs.readFileSync(f.file, 'utf8')).extensions[f.metadata.id]
  validatorState.unavailable = true
  const unavailable = f.store.snapshot({}).extensions[0]
  assert.equal(unavailable.id, f.metadata.id)
  assert.equal(unavailable.enabled, true)
  assert.equal(unavailable.manifest, null)
  assert.equal(unavailable.status, 'unavailable')
  assert.deepEqual(unavailable.values, {})
  assert.deepEqual(unavailable.secretConfigured, {})
  assert.ok(!JSON.stringify(unavailable).includes('preserved secret'))
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  f.store.update({}, { values: { AMADEUS_UI_THEME: 'classic' } })
  assert.deepEqual(JSON.parse(fs.readFileSync(f.file, 'utf8')).extensions[f.metadata.id], before)
  const launch = await f.installer.prepareLaunch()
  assert.deepEqual(JSON.parse(launch.extensions), [])
  assert.equal(launch.pendingRevisions[EXTENSIONS_ENV], undefined)
  assert.ok(f.store.pendingRevisionSnapshot()[EXTENSIONS_ENV])
  const deferredRevision = f.store.pendingRevisionSnapshot()[EXTENSIONS_ENV]
  await f.installer.prepareLaunch()
  assert.equal(f.store.pendingRevisionSnapshot()[EXTENSIONS_ENV], deferredRevision, 'unavailable retries must not churn revisions')
  await assert.rejects(f.installer.install(f.selectedWheel), /already installed/)
  assert.throws(() => f.store.update({}, { values: { TTS_BACKEND: `${f.metadata.id}/voice` } }), /Invalid backend identifier/)
  validatorState.unavailable = false
  const recovered = f.store.snapshot({}).extensions[0]
  assert.equal(recovered.manifest.id, f.metadata.id)
  assert.equal(recovered.enabled, true)
  assert.equal(recovered.values.streaming, false)
  assert.equal(recovered.secretConfigured.token, true)
  assert.equal(JSON.parse(await f.installer.launchSnapshot())[0].secrets.token, 'preserved secret')
  assert.ok(f.store.snapshot({}).restartRequired)
})

test('junction cleanup fails closed without traversing or deleting its target', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  const record = f.store.extensionRecords()[0]
  const directory = path.join(f.root, record.installId)
  const outside = path.join(f.directory, 'outside')
  fs.mkdirSync(outside)
  fs.writeFileSync(path.join(outside, 'keep'), 'protected')
  const link = path.join(directory, 'junction')
  fs.symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir')
  t.after(() => { if (fs.existsSync(link)) fs.unlinkSync(link) })
  assert.throws(() => removeExtensionDirectory(f.root, directory, record.installId), /link or junction/)
  assert.equal(fs.readFileSync(path.join(outside, 'keep'), 'utf8'), 'protected')
  assert.throws(() => assertExtensionPath(f.root, path.join(f.root, '..', 'outside')), /outside/)
  await f.installer.remove(record.id)
  await f.installer.cleanupStoppedBackend()
  assert.ok(fs.existsSync(directory))
  assert.ok(f.store.snapshot({}).extensionErrors.some(message => message.includes('retained')))
  const launch = await f.installer.prepareLaunch()
  assert.equal(launch.pendingRevisions[EXTENSIONS_ENV], undefined)
  f.store.markApplied({}, launch.pendingRevisions)
  assert.ok(f.store.snapshot({}).pendingRevisions[EXTENSIONS_ENV])
})

test('incompatible records and credentials survive reads, markApplied and orphan cleanup', async t => {
  const validatorState = { incompatible: false }
  const f = fixture(t, { validatorState })
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'preserved' } })
  const before = f.store.storedExtensionRecords()[0]
  validatorState.incompatible = true
  assert.equal(f.store.snapshot({}).extensions[0].status, 'incompatible')
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  f.store.markApplied({}, f.store.pendingRevisionSnapshot())
  f.store.update({}, { values: { AMADEUS_UI_THEME: 'classic' } })
  for (const file of [f.file, `${f.file}.bak`]) {
    assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).extensions[before.id], before)
  }
  await f.installer.cleanupStoppedBackend()
  assert.ok(fs.existsSync(path.join(f.root, before.installId)))
  assert.ok(f.store.extensionInstallationReferences().has(before.installId))
  validatorState.incompatible = false
  assert.equal(JSON.parse(await f.installer.launchSnapshot())[0].secrets.token, 'preserved')
  validatorState.incompatible = true
  await f.installer.remove(before.id)
  await f.installer.cleanupStoppedBackend()
  assert.deepEqual(f.store.storedExtensionRecords(), [])
})

for (const target of ['installation', 'data']) test(`partial ${target} deletion retains ownership and succeeds on retry`, async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'synthetic' } })
  const [entry] = JSON.parse(await f.installer.launchSnapshot())
  const record = f.store.extensionRecords()[0]
  const directory = target === 'installation' ? path.join(f.root, record.installId) : entry.data_path
  const marker = path.join(directory, `.amadeus-extension-${target === 'installation' ? 'install' : 'data'}.json`)
  const locked = path.join(directory, 'locked.txt')
  fs.writeFileSync(locked, 'temporary lock')
  const unlink = fs.unlinkSync
  let deny = true
  t.mock.method(fs, 'unlinkSync', file => {
    if (file === locked && deny) throw Object.assign(new Error('temporarily busy'), { code: 'EBUSY' })
    return unlink(file)
  })
  await f.installer.remove(record.id, { deleteData: true })
  await f.installer.cleanupStoppedBackend()
  assert.ok(fs.existsSync(marker))
  assert.equal(f.store.storedExtensionRecords()[0].pendingRemoval, true)
  deny = false
  await f.installer.cleanupStoppedBackend()
  assert.deepEqual(f.store.storedExtensionRecords(), [])
  assert.ok(!fs.existsSync(directory))
})

test('a junction above the installation root is allowed, links inside or at the root are refused', t => {
  const f = fixture(t)
  const actual = path.join(f.directory, 'actual')
  fs.mkdirSync(path.join(actual, 'extensions'), { recursive: true })
  const alias = path.join(f.directory, 'alias')
  fs.symlinkSync(actual, alias, process.platform === 'win32' ? 'junction' : 'dir')
  try {
    assert.doesNotThrow(() => assertExtensionPath(path.join(alias, 'extensions'), path.join(alias, 'extensions', 'new')))
    assert.throws(() => assertExtensionPath(alias, path.join(alias, 'new')), /root/)
  } finally { fs.unlinkSync(alias) }
})

test('corrupt extension credentials can be disabled, replaced, and cleared without exposing them', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } })
  const parsed = JSON.parse(fs.readFileSync(f.file, 'utf8'))
  parsed.extensions[f.metadata.id].encryptedSecrets.token = Buffer.from('broken').toString('base64')
  fs.writeFileSync(f.file, JSON.stringify(parsed))
  assert.ok(JSON.parse(await f.installer.launchSnapshot())[0].startup_error)
  const disabled = await f.installer.update({ id: f.metadata.id, enabled: false })
  assert.equal(disabled.extensions[0].enabled, false)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'replacement' } })
  assert.equal(JSON.parse(await f.installer.launchSnapshot())[0].secrets.token, 'replacement')
})

test('package data is retained by default and reused by reinstall without recreating installation trust', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } })
  const launch = JSON.parse(await f.installer.launchSnapshot())[0]
  fs.writeFileSync(path.join(launch.data_path, 'notes.json'), '{"note":"retained"}')
  const previousInstall = f.store.extensionRecords()[0].installId
  await f.installer.remove(f.metadata.id)
  await f.installer.cleanupStoppedBackend()
  assert.equal(fs.readFileSync(path.join(launch.data_path, 'notes.json'), 'utf8'), '{"note":"retained"}')
  assert.deepEqual(f.store.extensionRecords(), [])
  assert.deepEqual(JSON.parse(await f.installer.launchSnapshot()), [])
  await f.installer.install(f.selectedWheel)
  assert.notEqual(f.store.extensionRecords()[0].installId, previousInstall)
  assert.equal(f.store.snapshot({}).extensions[0].enabled, false)
  assert.equal(f.store.snapshot({}).extensions[0].secretConfigured.token, false)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'new secret' } })
  assert.equal(JSON.parse(await f.installer.launchSnapshot())[0].data_path, launch.data_path)
})

test('explicit package-data deletion survives restart and occurs only with ownership verification', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } })
  const launch = JSON.parse(await f.installer.launchSnapshot())[0]
  fs.writeFileSync(path.join(launch.data_path, 'notes.json'), '{}')
  await f.installer.remove(f.metadata.id, { deleteData: true })
  assert.equal(f.store.extensionRecords()[0].deleteDataOnRemoval, true)
  assert.ok(fs.existsSync(launch.data_path))
  const restartedStore = new DesktopSettingsStore(f.file, path.join(f.directory, '.env'), process.platform, () => true)
  const restarted = new ExtensionInstaller(f.root, f.directory, restartedStore, {}, () => 'trusted-python', {}, async () => '')
  await restarted.cleanupStoppedBackend()
  assert.ok(!fs.existsSync(launch.data_path))
  assert.deepEqual(restartedStore.extensionRecords(), [])
})

test('explicit data deletion refuses junctions and wrong ownership without affecting unrelated data', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } })
  const launch = JSON.parse(await f.installer.launchSnapshot())[0]
  const outside = path.join(f.directory, 'unrelated-data')
  fs.mkdirSync(outside)
  fs.writeFileSync(path.join(outside, 'keep'), 'safe')
  const link = path.join(launch.data_path, 'junction')
  fs.symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir')
  t.after(() => { if (fs.existsSync(link)) fs.unlinkSync(link) })
  await f.installer.remove(f.metadata.id, { deleteData: true })
  await f.installer.cleanupStoppedBackend()
  assert.equal(fs.readFileSync(path.join(outside, 'keep'), 'utf8'), 'safe')
  assert.ok(fs.existsSync(launch.data_path))
  assert.equal(f.store.extensionRecords()[0].pendingRemoval, true)
  fs.unlinkSync(link)
  fs.writeFileSync(path.join(launch.data_path, '.amadeus-extension-data.json'), '{"id":"different.package"}')
  await f.installer.cleanupStoppedBackend()
  assert.ok(fs.existsSync(launch.data_path))
  assert.equal(f.store.extensionRecords()[0].pendingRemoval, true)
})

test('missing enabled package reports a startup error without secrets or blocking ordinary backend launch', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'secret' } })
  const record = f.store.extensionRecords()[0]
  fs.rmSync(path.join(f.root, record.installId, 'venv', ...(process.platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python'])))
  const snapshot = JSON.parse(await f.installer.launchSnapshot())[0]
  assert.equal(snapshot.enabled, true)
  assert.ok(snapshot.startup_error)
  assert.deepEqual(snapshot.secrets, {})
  assert.equal(f.store.snapshot({}).extensions[0].enabled, true)
})

test('trusted installer transport environment excludes unrelated credentials and injected extension snapshots', () => {
  const env = extensionInstallerEnvironment({ PATH: 'python', PIP_INDEX_URL: 'https://user:password@example.invalid/simple',
    HTTPS_PROXY: 'https://proxy.invalid', DEEPSEEK_API_KEY: 'private', AMADEUS_EXTENSIONS: 'private snapshot', PYTHONPATH: 'untrusted' })
  assert.equal(env.PIP_INDEX_URL, 'https://user:password@example.invalid/simple')
  assert.equal(env.HTTPS_PROXY, 'https://proxy.invalid')
  assert.equal(env.DEEPSEEK_API_KEY, undefined)
  assert.equal(env.AMADEUS_EXTENSIONS, undefined)
  assert.equal(env.PYTHONPATH, undefined)
})

test('trusted record validation is asynchronous, isolated and cached; reads never spawn Python', async () => {
  const root = fileURLToPath(new URL('../../', import.meta.url))
  const invocations = []
  let release
  const helper = new ExtensionPackageHelper(root, () => 'trusted-python', { PATH: 'runtime', SECRET_TOKEN: 'private', AMADEUS_EXTENSIONS: 'injected' },
    async (command, args, options) => { invocations.push({ command, args, options }); return new Promise(resolve => { release = resolve }) })
  const values = { endpoint: 'https://example.invalid/api', streaming: true }
  assert.equal(helper.validateRecord(manifest(), values), 'unavailable')
  assert.equal(invocations.length, 0)
  const refresh = helper.refreshRecords([{ manifest: manifest(), values }])
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(helper.validateRecord(manifest(), values), 'unavailable')
  release('{"ok":true}')
  await refresh
  assert.equal(helper.validateRecord(manifest(), { endpoint: 'https://example.invalid/api', streaming: true }), true)
  assert.equal(helper.validateRecord(manifest(), { endpoint: 'https://example.invalid/api', streaming: true }), true)
  assert.equal(invocations.length, 1)
  assert.deepEqual(invocations[0].args, ['-I', path.join(root, 'tools', 'extension_package.py'), 'validate-config'])
  assert.equal(invocations[0].options.env.SECRET_TOKEN, undefined)
  assert.equal(invocations[0].options.env.AMADEUS_EXTENSIONS, undefined)
  assert.equal(JSON.parse(invocations[0].options.input).allow_incomplete, true)
})


test('SDK integrity failure and incompatible wheel stop before confirmation or pip', async t => {
  const damaged = fixture(t)
  fs.writeFileSync(path.join(damaged.directory, 'vendor/extension-sdk/amadeus_extension_sdk-0.1.0-py3-none-any.whl'), 'damaged')
  await assert.rejects(damaged.installer.install(damaged.selectedWheel), /SHA-256 verification/)
  assert.deepEqual(damaged.commandCalls, [])
  assert.ok(!damaged.calls.some(call => call.command === 'confirm'))
  assert.deepEqual(damaged.store.storedExtensionRecords(), [])
  const incompatible = fixture(t, { inspectionError: 'extension requires SDK >=0.2; Amadeus provides 0.1.0' })
  await assert.rejects(incompatible.installer.install(incompatible.selectedWheel), /requires SDK >=0.2/)
  assert.deepEqual(incompatible.commandCalls, [])
  assert.ok(!incompatible.calls.some(call => call.command === 'confirm'))
})

test('installation disclosure uses inspected bytes and source basename', async t => {
  const f = fixture(t)
  f.metadata.license = 'MIT-0'
  await f.installer.install(f.selectedWheel)
  const review = f.calls.find(call => call.command === 'confirm').review
  assert.equal(review.digest, createHash('sha256').update('synthetic wheel bytes').digest('hex'))
  assert.equal(review.sourceFile, path.basename(f.selectedWheel))
  const { extensionDisclosure } = loadTypeScript(new URL('../src/shared/extensions.ts', import.meta.url))
  const text = extensionDisclosure(review.manifest, review)
  assert.ok(text.includes('License: MIT-0') && text.includes(review.digest) && text.includes(review.sourceFile))
  assert.ok(!text.includes(f.directory))
  delete f.metadata.license
  assert.ok(extensionDisclosure(f.metadata).includes('License: Not declared'))
})

test('installer selects SDK artifact from the pinned manifest instead of a fixed filename', async t => {
  const f = fixture(t)
  const provenance = path.join(f.directory, 'server/extensions/protocol-source.json')
  const pin = JSON.parse(fs.readFileSync(provenance, 'utf8'))
  const old = path.join(f.directory, 'vendor/extension-sdk', pin.sdk.filename)
  pin.sdk.version = '0.1.7'
  pin.sdk.filename = 'amadeus_extension_sdk-0.1.7-py3-none-any.whl'
  fs.renameSync(old, path.join(path.dirname(old), pin.sdk.filename))
  fs.writeFileSync(provenance, JSON.stringify(pin))
  await f.installer.install(f.selectedWheel)
  assert.ok(f.commandCalls.some(call => call.args.at(-1).endsWith(pin.sdk.filename)))
  assert.ok(!f.commandCalls.some(call => call.args.at(-1).endsWith('amadeus_extension_sdk-0.1.0-py3-none-any.whl')))
})


test('a new launch retires logical backend receipts without authorizing file cleanup', async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'token' } })
  const v1 = await f.installer.prepareLaunch()
  assert.equal(await f.installer.acceptLaunch(v1, { [f.metadata.id]: 2 }), true)
  const old = f.store.appliedExtensionRecords()[0]
  await f.installer.update({ id: f.metadata.id, values: { streaming: false } })
  const v2 = await f.installer.prepareLaunch() // unconfirmed old child stop: no cleanup call
  assert.deepEqual(f.store.appliedExtensionRecords(), [])
  assert.equal(await f.installer.acceptLaunch(v2, {}), false)
  assert.equal(f.store.snapshot({}).extensions[0].appliedVersion, undefined)
  assert.ok(fs.existsSync(path.join(f.root, old.installId)))
})

for (const blocked of ['credentials', 'installation', 'configuration']) test(`handled ${blocked} startup failure does not demand repeated restarts`, async t => {
  const f = fixture(t)
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: f.metadata.id, enabled: true, secrets: { token: 'token' } })
  const record = f.store.extensionRecords()[0]
  if (blocked === 'credentials') {
    const saved = JSON.parse(fs.readFileSync(f.file, 'utf8'))
    saved.extensions[record.id].encryptedSecrets.token = Buffer.from('bad ciphertext').toString('base64')
    fs.writeFileSync(f.file, JSON.stringify(saved))
  } else if (blocked === 'installation') {
    fs.renameSync(path.join(f.root, record.installId), path.join(f.root, record.installId + '-missing'))
  } else f.store.saveExtension({}, record, { token: null })
  const launch = await f.installer.prepareLaunch()
  assert.ok(JSON.parse(launch.extensions)[0].startup_error)
  assert.equal(await f.installer.acceptLaunch(launch, {}), true)
  f.store.markApplied({}, launch.pendingRevisions)
  assert.equal(f.store.snapshot({}).restartRequired, false)
  assert.equal(f.store.snapshot({}).extensions[0].appliedRevision, undefined)
  await f.installer.update({ id: record.id, enabled: false })
  f.store.markApplied({}, launch.pendingRevisions)
  assert.equal(f.store.snapshot({}).restartRequired, true, 'an old blocked receipt must not acknowledge a newer edit')
})

test('unavailable or pending-removal frontend-only records do not create backend restart work', async t => {
  const metadata = manifest()
  metadata.runtime = { kind: 'none' }
  metadata.contributes = { theme: [{ id: 'light', base: 'classic', contract: 'amadeus.ui.theme/v0', tokens: {} }] }
  const validatorState = { unavailable: false }
  const f = fixture(t, { manifest: metadata, validatorState })
  await f.installer.install(f.selectedWheel)
  await f.installer.update({ id: metadata.id, enabled: true, secrets: { token: 'token' } })
  validatorState.unavailable = true
  await f.installer.prepareLaunch()
  assert.equal(f.store.snapshot({}).restartRequired, false)
  validatorState.unavailable = false
  await f.installer.remove(metadata.id)
  await f.installer.prepareLaunch()
  assert.equal(f.store.snapshot({}).restartRequired, false)
})
