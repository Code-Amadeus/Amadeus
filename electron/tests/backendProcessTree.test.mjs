import assert from 'node:assert/strict'
import { EventEmitter, once } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import { spawn, spawnSync } from 'node:child_process'
import path from 'node:path'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const { forceStopBackendTree, stopOwnedBackend, waitForProcessExit, prepareBackendCleanup: preparePlatformBackendCleanup,
  verifyPreviousBackendCleanup: verifyCleanup, markBackendCleanupUnconfirmed: markUnconfirmed, resolveBackendProjectRoot,
  readWindowsBootId } = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url))

// Receipt protocol fixtures exercise Windows policy even when Node is running
// elsewhere; the POSIX routing cases explicitly supply their own platform.
const fixtureBoot = async () => 10
const prepareBackendCleanup = (file, launch, platform = 'win32', boot = fixtureBoot) => preparePlatformBackendCleanup(file, launch, platform, boot)
const verifyPreviousBackendCleanup = (file, options = {}) => verifyCleanup(file, {getBootId: fixtureBoot, ...options})
const markBackendCleanupUnconfirmed = (file, options = {}) => markUnconfirmed(file, {getBootId: fixtureBoot, ...options})

test('packaged cleanup resolves the supplied Python module root with the actual launch environment', async () => {
  const resources = path.resolve('packaged/resources'), backend = path.resolve('external/backend')
  const environment = { PYTHONPATH: backend }
  const root = await resolveBackendProjectRoot('supplied-python', resources, environment, (python, args, options, done) => {
    assert.equal(python, 'supplied-python')
    assert.equal(args[0], '-c')
    assert.ok(args[1].includes("find_spec('server')"))
    assert.equal(options.cwd, resources)
    assert.equal(options.env, environment)
    assert.equal(options.windowsHide, true)
    done(null, JSON.stringify(backend), '')
  })
  assert.equal(root, backend)
})

test('an unresolved backend root never falls back to the frontend resource directory', async () => {
  for (const output of ['null', '"relative"', 'not JSON']) {
    await assert.rejects(resolveBackendProjectRoot('python', process.cwd(), {}, (_python, _args, _options, done) => done(null, output, '')))
  }
  await assert.rejects(resolveBackendProjectRoot('python', process.cwd(), {}, (_python, _args, _options, done) => done(Error('missing backend'))), /missing backend/)
})

test('forced Windows backend stop targets only its owned PID and whole descendant tree without Host secrets', async () => {
  const invocations = []
  const spawn = (command, args, options) => {
    invocations.push({ command, args, options })
    const child = new EventEmitter()
    child.kill = () => {}
    queueMicrotask(() => child.emit('close', 0))
    return child
  }
  await forceStopBackendTree({ pid: 12345 }, 'win32', spawn)
  assert.equal(path.basename(invocations[0].command), 'taskkill.exe')
  assert.deepEqual(invocations[0].args, ['/PID', '12345', '/T', '/F'])
  assert.deepEqual(Object.keys(invocations[0].options.env).sort(), ['SystemRoot', 'WINDIR'])
  assert.equal(invocations[0].options.windowsHide, true)
})

test('failed process-tree shutdown is observable and cannot authorize uninstall cleanup', async () => {
  const spawn = () => {
    const child = new EventEmitter()
    child.kill = () => {}
    queueMicrotask(() => child.emit('close', 1))
    return child
  }
  await assert.rejects(forceStopBackendTree({ pid: 12345 }, 'win32', spawn), /could not be confirmed.*retained/)
  await assert.rejects(forceStopBackendTree({ pid: 0 }, 'win32', spawn), /identity/)
})

test('an unavailable registered Job falls back only to the owned process tree', async () => {
  const {forceStopBackendTree: force} = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url), {
    'node:child_process': {execFile: (_python, args, options, done) => {
      assert.equal(args[3], '--terminate')
      assert.deepEqual(Object.keys(options.env).sort(), ['SystemRoot', 'WINDIR'])
      queueMicrotask(() => done(Error('Job unavailable')))
    }},
  })
  let fallback = 0
  const create = (command, args) => {
    fallback++
    assert.equal(path.basename(command), 'taskkill.exe')
    assert.deepEqual(args, ['/PID', '12345', '/T', '/F'])
    const child = Object.assign(new EventEmitter(), {kill() {}})
    queueMicrotask(() => child.emit('close', 0))
    return child
  }
  await force({pid: 12345, exitCode: null, signalCode: null}, 'win32', create, JSON.stringify({
    schema: 'amadeus.backend-cleanup.v1', owner: {id: 'a'.repeat(32), nonce: 'synthetic-launch-nonce',
      project_root: path.resolve('..'), python_command: 'python'},
  }))
  assert.equal(fallback, 1)
})

function processFixture() {
  const proc = new EventEmitter()
  Object.assign(proc, { pid: 12345, exitCode: null, signalCode: null })
  proc.exit = () => { proc.exitCode = 0; proc.emit('exit', 0) }
  return proc
}

for (const platform of ['linux', 'darwin']) {
  test(`${platform} preserves graceful root shutdown without claiming Windows cleanup proof`, async t => {
    const file = cleanupFixture(t)
    assert.deepEqual(await prepareBackendCleanup(file, {nonce: 'synthetic-owner-nonce', projectRoot: path.dirname(file), pythonCommand: 'python'}, platform), {environment: {}})
    assert.equal(fs.existsSync(file), false)
    const proc = processFixture()
    assert.equal(await stopOwnedBackend(proc, async () => true, {platform,
      waitForExit: async (_proc, timeout) => {assert.equal(timeout, 2500); proc.exit(); return true},
      verifyCleanup: async () => {assert.fail('POSIX must not claim a Windows receipt')},
    }), true)
    const forced = processFixture()
    assert.equal(await stopOwnedBackend(forced, async () => false, {platform,
      forceStop: async () => {forced.exit()}, waitForExit: async () => true,
    }), false)
  })
}

test('graceful owned shutdown requires independent process-tree exit evidence', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => true, { platform: 'win32',
    waitForExit: async (_proc, timeout) => { assert.equal(timeout, 15000); proc.exit(); return true },
    forceStop: async () => { assert.fail('graceful stop must not force the tree') },
    verifyCleanup: async () => true,
  }), true)
  const noReceipt = processFixture()
  assert.equal(await stopOwnedBackend(noReceipt, async () => true, { platform: 'win32',
    waitForExit: async () => { noReceipt.exit(); return true },
  }), false)
})

test('forced Windows shutdown stops the root without authorizing package replacement', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => false, { platform: 'win32',
    forceStop: async owned => { assert.equal(owned, proc); proc.exit() },
    waitForExit: async () => true,
    verifyCleanup: async () => { assert.fail('forced exit cannot use a cleanup receipt') },
  }), false)
})

test('an exit racing failed force-stop reports uncertain children instead of a false running backend', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => false, {
    forceStop: async () => { proc.exit(); throw Error('PID has already exited') },
  }), false)
  const alreadyExited = processFixture(); alreadyExited.exit()
  assert.equal(await stopOwnedBackend(alreadyExited, async () => false, {
    forceStop: async () => { assert.fail('exited PID must not be targeted') },
  }), false)
})

test('a still-running backend fails explicitly and can be stopped on retry', async () => {
  const proc = processFixture()
  await assert.rejects(stopOwnedBackend(proc, async () => false, {
    forceStop: async () => { throw Error('termination denied') },
  }), /termination denied/)
  await assert.rejects(stopOwnedBackend(proc, async () => false, {
    forceStop: async () => {}, waitForExit: async () => false,
  }), /shutdown did not complete/)
  assert.equal(await stopOwnedBackend(proc, async () => false, { platform: 'win32',
    forceStop: async () => proc.exit(), waitForExit: async () => true,
  }), false)
})

test('non-Windows forced root exit does not claim descendant lifetime', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => false, { platform: 'linux',
    forceStop: async () => proc.exit(), waitForExit: async () => true,
  }), false)
})

test('exit observation releases its listener on success and timeout', async () => {
  const proc = processFixture()
  assert.equal(await waitForProcessExit(proc, 1), false)
  assert.equal(proc.listenerCount('exit'), 0)
  const result = waitForProcessExit(proc, 1000)
  proc.exit()
  assert.equal(await result, true)
  assert.equal(proc.listenerCount('exit'), 0)
})

function cleanupFixture(t) {
  // macOS exposes its temporary directory through /var -> /private/var.
  // Exercise cleanup's canonical-path contract without weakening its link guard.
  const directory = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-cleanup-')))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  return path.join(directory, 'backend-cleanup.json')
}

function writeReceipt(file, environment, changes = {}) {
  const launch = JSON.parse(environment.AMADEUS_BACKEND_CLEANUP_LAUNCH)
  const receipt = { schema: launch.schema, owner: launch.owner, state: 'settled',
    process: { pid: 12345, created: '133000000000000000' },
    observer: { pid: 23456, created: '133000000000000001' },
    job_name: `Global\\AmadeusBackendCleanup-${launch.owner.id}`,
    proof: 'windows-job-drained', providers_settled: true, work_settled: true, ...changes }
  fs.writeFileSync(path.join(`${file}.receipts`, `${launch.owner.id}.json`), JSON.stringify(receipt))
  return receipt
}

const launchIdentity = { nonce: 'synthetic-launch-nonce', projectRoot: path.resolve('..'), pythonCommand: 'python' }
const goneAndDrained = { isProcessIdentityGone: async () => true }

test('new-protocol fresh state differs from a persisted unknown reused backend', async t => {
  const file = cleanupFixture(t)
  assert.equal(await verifyPreviousBackendCleanup(file), true)
  await markBackendCleanupUnconfirmed(file)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
})

test('cleanup refuses linked state paths without changing the target index', async t => {
  const file = cleanupFixture(t)
  await markBackendCleanupUnconfirmed(file)
  const before = fs.readFileSync(file, 'utf8')
  const link = path.join(path.dirname(file), 'linked')
  fs.symlinkSync(path.dirname(file), link, process.platform === 'win32' ? 'junction' : 'dir')
  const linkedFile = path.join(link, path.basename(file))
  try {
    await assert.rejects(prepareBackendCleanup(linkedFile, launchIdentity), /cannot traverse a link/)
    await assert.rejects(markBackendCleanupUnconfirmed(linkedFile), /cannot traverse a link/)
    assert.equal(await verifyPreviousBackendCleanup(linkedFile, goneAndDrained), false)
    assert.equal(fs.readFileSync(file, 'utf8'), before)
  } finally { fs.unlinkSync(link) }
})

test('cleanup refuses a linked receipt directory within an ordinary data root', async t => {
  const file = cleanupFixture(t)
  const target = path.join(path.dirname(file), 'outside-receipts')
  fs.mkdirSync(target)
  const link = `${file}.receipts`
  fs.symlinkSync(target, link, process.platform === 'win32' ? 'junction' : 'dir')
  try {
    await assert.rejects(prepareBackendCleanup(file, launchIdentity))
    assert.equal(await verifyPreviousBackendCleanup(file), false)
    assert.deepEqual(fs.readdirSync(target), [])
  } finally { fs.unlinkSync(link) }
})

test('a failed durable Windows replacement preserves the original index', { skip: process.platform !== 'win32' }, async t => {
  const file = cleanupFixture(t)
  fs.writeFileSync(file, JSON.stringify({schema: 'amadeus.backend-cleanup.v1', bootId: 10, unknown: false, owners: []}))
  const before = fs.readFileSync(file, 'utf8')
  // A read-only destination permits the index read but denies MoveFileExW.
  fs.chmodSync(file, 0o444)
  try {
    await assert.rejects(prepareBackendCleanup(file, launchIdentity), error => error.code === 3)
    assert.equal(fs.readFileSync(file, 'utf8'), before)
    assert.equal(fs.existsSync(`${file}.receipts`), false)
  } finally { fs.chmodSync(file, 0o600) }
  const fresh = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url))
  assert.equal(await fresh.verifyPreviousBackendCleanup(file, {getBootId: fixtureBoot}), false,
    'an interrupted registration must fence a later desktop even when the old empty index is readable')
})

test('a later clean launch cannot erase unresolved containment from the same boot', async t => {
  const file = cleanupFixture(t)
  await prepareBackendCleanup(file, launchIdentity)
  const second = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, second.environment)
  assert.equal(JSON.parse(fs.readFileSync(file)).owners.length, 2)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
})

test('a proven later Windows boot retires unresolved owners and unknown state without modifying archived evidence', async t => {
  const file = cleanupFixture(t)
  const launch = await prepareBackendCleanup(file, launchIdentity)
  const old = JSON.parse(launch.environment.AMADEUS_BACKEND_CLEANUP_LAUNCH).owner
  const diagnostic = path.join(`${file}.receipts`, `${old.id}.failed.json`)
  fs.writeFileSync(diagnostic, 'retained failed-teardown evidence')
  await markBackendCleanupUnconfirmed(file)
  const bytes = fs.readFileSync(file, 'utf8')
  for (const getBootId of [fixtureBoot, async () => undefined, async () => 9, async () => { throw Error('unavailable') }]) {
    assert.equal(await verifyPreviousBackendCleanup(file, {getBootId}), false)
    assert.equal(fs.readFileSync(file, 'utf8'), bytes)
  }
  assert.equal(await verifyPreviousBackendCleanup(file, {getBootId: async () => 11,
    isProcessIdentityGone: async () => assert.fail('previous-boot PIDs are not current owners'),
  }), true)
  assert.deepEqual(JSON.parse(fs.readFileSync(file)), {schema: 'amadeus.backend-cleanup.v1', bootId: 11, unknown: false, owners: []})
  assert.equal(fs.readFileSync(diagnostic, 'utf8'), 'retained failed-teardown evidence')
  await prepareBackendCleanup(file, launchIdentity, 'win32', async () => 11)
  assert.equal(await verifyPreviousBackendCleanup(file, {getBootId: async () => 11}), false, 'new-boot registration still needs its own exit proof')
})

test('legacy unresolved owners acquire a boot binding without being silently cleared', async t => {
  const file = cleanupFixture(t)
  await prepareBackendCleanup(file, launchIdentity, 'win32', async () => undefined)
  assert.equal(JSON.parse(fs.readFileSync(file)).bootId, undefined)
  assert.equal(await verifyPreviousBackendCleanup(file), false)
  assert.equal(JSON.parse(fs.readFileSync(file)).bootId, 10)
  assert.equal(JSON.parse(fs.readFileSync(file)).owners.length, 1)
  assert.equal(await verifyPreviousBackendCleanup(file), false)
  assert.equal(await verifyPreviousBackendCleanup(file, {getBootId: async () => 11}), true)
})

test('physical exit evidence is independent of unsettled Work outcomes but still requires exact process identities', async t => {
  const file = cleanupFixture(t)
  const {environment} = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, environment, {state: 'exited', providers_settled: false, work_settled: false})
  assert.equal(await verifyPreviousBackendCleanup(file, {isProcessIdentityGone: async () => false}), false)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), true)
})

test('Windows boot reader rejects unavailable or malformed registry evidence and caches only a valid DWORD', {skip: process.platform !== 'win32'}, async t => {
  const module = new URL('../src/main/backendProcessTree.ts', import.meta.url)
  const replies = [
    {error: Error('registry key unavailable')},
    {error: Object.assign(Error('query timed out'), {killed: true})},
    {output: ''},
    {output: '    BootId    REG_SZ    0x20\r\n'},
    {output: '    BootId    REG_DWORD    0x100000000\r\n'},
    {output: '    BootId    REG_DWORD    0x20\r\n    BootId    REG_DWORD    0x21\r\n'},
  ]
  for (const reply of replies) {
    let calls = 0
    const {readWindowsBootId: read} = loadTypeScript(module, {'node:child_process': {execFile: (command, args, options, done) => {
      calls++
      assert.equal(path.basename(command).toLowerCase(), 'reg.exe')
      assert.deepEqual(args.slice(-3), ['/v', 'BootId', '/reg:64'])
      assert.equal(options.windowsHide, true)
      queueMicrotask(() => done(reply.error || null, reply.output || ''))
    }}})
    assert.equal(await read(), undefined)
    const file = cleanupFixture(t)
    await markBackendCleanupUnconfirmed(file)
    const before = fs.readFileSync(file)
    assert.equal(await verifyPreviousBackendCleanup(file, {getBootId: read}), false)
    assert.deepEqual(fs.readFileSync(file), before)
    assert.equal(calls, 2, 'unavailable evidence must not be cached or grant retirement')
  }
  for (const [hex, expected] of [['0x0', 0], ['0x2a', 42], ['0xffffffff', 0xffffffff]]) {
    let calls = 0
    const {readWindowsBootId: read} = loadTypeScript(module, {'node:child_process': {execFile: (_command, _args, _options, done) => {
      calls++
      queueMicrotask(() => done(null, `HKEY_LOCAL_MACHINE\\fixture\r\n    BootId    REG_DWORD    ${hex}\r\n`))
    }}})
    assert.equal(await read(), expected)
    assert.equal(await read(), expected)
    assert.equal(calls, 1)
  }
})

test('native Windows boot observation supplies stable evidence or leaves unknown ownership fenced', {skip: process.platform !== 'win32'}, async t => {
  const first = await readWindowsBootId()
  if (first !== undefined) {
    assert.ok(Number.isSafeInteger(first) && first >= 0 && first <= 0xffffffff)
    assert.equal(await readWindowsBootId(), first)
  } else {
    t.diagnostic('OS boot counter is unavailable; verifying conservative ownership retention')
  }
  const file = cleanupFixture(t)
  await markUnconfirmed(file, {getBootId: readWindowsBootId})
  assert.equal(await verifyCleanup(file, {getBootId: readWindowsBootId}), false)
  const retained = JSON.parse(fs.readFileSync(file))
  assert.equal(retained.unknown, true)
  assert.deepEqual(retained.owners, [])
})

test('clean receipt must bind nonce, launch and PID creation, and a fully drained Job', async t => {
  const file = cleanupFixture(t)
  const { environment } = await prepareBackendCleanup(file, launchIdentity)
  const good = writeReceipt(file, environment)
  assert.equal(await verifyPreviousBackendCleanup(file, { ...goneAndDrained, isProcessIdentityGone: async () => false }), false)
  assert.equal(await verifyPreviousBackendCleanup(file, { isProcessIdentityGone: async identity => identity.pid !== 23456 }), false)
  assert.equal(await verifyPreviousBackendCleanup(file, { isProcessIdentityGone: async () => { throw Error('access denied') } }), false)
  for (const changes of [
    { owner: { ...good.owner, nonce: 'other-instance-nonce' } },
    { owner: { ...good.owner, python_command: 'other-python' } },
    { process: { pid: 12345 } }, { observer: { pid: 23456 } }, { work_settled: false }, { providers_settled: false },
    { job_name: 'other-job' }, { state: 'unknown' },
  ]) {
    writeReceipt(file, environment, changes)
    assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
  }
  writeReceipt(file, environment)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), true)
})

test('verified history is compacted once while receipt archives stay unchanged', async t => {
  const file = cleanupFixture(t)
  const archived = []
  for (let attempt = 0; attempt < 3; attempt++) {
    const { environment } = await prepareBackendCleanup(file, launchIdentity)
    const receipt = writeReceipt(file, environment)
    const receiptPath = path.join(`${file}.receipts`, `${receipt.owner.id}.json`)
    archived.push([receiptPath, fs.readFileSync(receiptPath, 'utf8')])
  }
  let probes = 0
  const options = { isProcessIdentityGone: async () => { probes++; return true } }
  assert.equal(await verifyPreviousBackendCleanup(file, options), true)
  assert.equal(probes, 6)
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).owners, [])
  assert.equal(await verifyPreviousBackendCleanup(file, options), true)
  assert.equal(probes, 6, 'a repeated shutdown scan must not relaunch historical PID probes')
  for (const [receiptPath, bytes] of archived) assert.equal(fs.readFileSync(receiptPath, 'utf8'), bytes)
})

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('compaction cannot overwrite a new owner queued during identity verification', async t => {
  const file = cleanupFixture(t)
  const first = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, first.environment)
  const entered = deferred(), release = deferred()
  const checking = verifyPreviousBackendCleanup(file, { isProcessIdentityGone: async () => {
    entered.resolve(); await release.promise; return true
  } })
  await entered.promise
  const adding = prepareBackendCleanup(file, launchIdentity)
  release.resolve()
  assert.equal(await checking, true)
  const second = JSON.parse((await adding).environment.AMADEUS_BACKEND_CLEANUP_LAUNCH)
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).owners, [second.owner])
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false, 'the new owner has no cleanup receipt')
})

test('compaction cannot erase an unknown fence queued during identity verification', async t => {
  const file = cleanupFixture(t)
  const { environment } = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, environment)
  const entered = deferred(), release = deferred()
  const checking = verifyPreviousBackendCleanup(file, { isProcessIdentityGone: async () => {
    entered.resolve(); await release.promise; return true
  } })
  await entered.promise
  const marking = markBackendCleanupUnconfirmed(file)
  release.resolve()
  assert.equal(await checking, true)
  await marking
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), {
    schema: 'amadeus.backend-cleanup.v1', bootId: 10, unknown: true, owners: [],
  })
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
})

test('concurrent registrations and unknown marking retain every new owner and fence', async t => {
  const file = cleanupFixture(t)
  const [first, , second] = await Promise.all([
    prepareBackendCleanup(file, launchIdentity), markBackendCleanupUnconfirmed(file),
    prepareBackendCleanup(file, launchIdentity),
  ])
  const owners = [first, second].map(result => JSON.parse(result.environment.AMADEUS_BACKEND_CLEANUP_LAUNCH).owner)
  const index = JSON.parse(fs.readFileSync(file, 'utf8'))
  assert.equal(index.unknown, true)
  assert.deepEqual(index.owners, owners)
  for (const owner of owners) assert.equal(fs.existsSync(path.join(`${file}.receipts`, `${owner.id}.json`)), false)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
})

test('missing or corrupt proof leaves all live index bytes unchanged', async t => {
  const file = cleanupFixture(t)
  const first = await prepareBackendCleanup(file, launchIdentity)
  const second = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, first.environment)
  const before = fs.readFileSync(file, 'utf8')
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
  assert.equal(fs.readFileSync(file, 'utf8'), before)
  const bad = writeReceipt(file, second.environment, { work_settled: false })
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
  assert.equal(fs.readFileSync(file, 'utf8'), before)
  writeReceipt(file, second.environment, { process: { ...bad.process, created: '' } })
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
  assert.equal(fs.readFileSync(file, 'utf8'), before)
})

test('corrupt and missing-index receipt state never become fresh', async t => {
  const file = cleanupFixture(t)
  const { environment } = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, environment)
  fs.unlinkSync(file)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
  fs.writeFileSync(file, '{broken')
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
  await assert.rejects(prepareBackendCleanup(file, launchIdentity))
})

function existingPython(root) {
  const local = path.join(root, '.venv', 'Scripts', 'python.exe')
  const command = process.env.AMADEUS_TEST_PYTHON || (fs.existsSync(local) ? local : 'python')
  // These probes import only the standard-library cleanup module. CI can use
  // its installed Python without creating the application's virtualenv.
  const result = spawnSync(command, ['-I', '-c', 'import sys; print(sys.executable)'], {
    encoding: 'utf8', timeout: 10000, windowsHide: true,
  })
  assert.equal(result.status, 0, result.error?.message || result.stderr)
  return result.stdout.trim()
}

for (const graceful of [true, false]) test(`real Windows Job retains a surviving child with graceful approval=${graceful}`, { skip: process.platform !== 'win32' }, async t => {
  const file = cleanupFixture(t)
  const root = path.resolve('..')
  const python = existingPython(root)
  const { environment } = await prepareBackendCleanup(file, { ...launchIdentity, projectRoot: root, pythonCommand: python })
  const release = path.join(path.dirname(file), 'release-child')
  const started = path.join(path.dirname(file), 'child-started')
  const helper = path.join(path.dirname(file), 'job-helper.py')
  const childSource = `import pathlib,time; p=pathlib.Path(${JSON.stringify(release)}); pathlib.Path(${JSON.stringify(started)}).write_text('started'); end=time.monotonic()+20\nwhile not p.exists() and time.monotonic()<end: time.sleep(.02)`
  fs.writeFileSync(helper, `import json,os,subprocess,sys,time\nfrom pathlib import Path\nsys.path.insert(0,${JSON.stringify(root)})\nfrom server.backend_cleanup import BackendCleanup\nlaunch=json.loads(os.environ['AMADEUS_BACKEND_CLEANUP_LAUNCH'])\nproof=BackendCleanup(launch)\nif proof.failure:\n print(json.dumps({'failure':proof.failure}));sys.exit(3)\n${graceful ? 'proof.record_runtime_cleanup(providers_settled=True,work_settled=True,teardown_succeeded=True)\nassert proof.finish()' : '# No graceful approval; child exit must still be proved.'}\n# Model a late finalizer child after the backend's last observation.\nchild=subprocess.Popen([sys.executable,'-c',${JSON.stringify(childSource)}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nend=time.monotonic()+5\nwhile not Path(${JSON.stringify(started)}).exists() and time.monotonic()<end: time.sleep(.02)\nassert Path(${JSON.stringify(started)}).exists(), f'child did not start: {child.poll()}'\nassert not proof.job.only_backend_remains(), 'Job must include the live child'\nprint(json.dumps({'child':child.pid,'root':os.getpid()}))\n`)
  const result = spawnSync(python, ['-B', helper], { cwd: root, env: { ...process.env, ...environment }, encoding: 'utf8', timeout: 10000, windowsHide: true })
  assert.equal(result.status, 0, result.stdout + result.stderr)
  const identities = JSON.parse(result.stdout.trim())
  try {
    assert.equal(await verifyPreviousBackendCleanup(file), false, 'a dead parent does not prove its live child has exited')
  } finally { fs.writeFileSync(release, '') }
  let clean = false
  for (let attempt = 0; attempt < 12 && !clean; attempt++) {
    clean = await verifyPreviousBackendCleanup(file)
    if (!clean) await new Promise(resolve => setTimeout(resolve, 50))
  }
  assert.equal(clean, true, `owned child ${identities.child} must drain normally after release`)
})

for (const outcome of ['crash', 'unsettled-work']) test(`real ${outcome} is recoverable after the Job drains without granting Work success`, { skip: process.platform !== 'win32' }, async t => {
  const file = cleanupFixture(t)
  const root = path.resolve('..')
  const python = existingPython(root)
  const { environment } = await prepareBackendCleanup(file, { ...launchIdentity, projectRoot: root, pythonCommand: python })
  const launch = JSON.parse(environment.AMADEUS_BACKEND_CLEANUP_LAUNCH)
  const helper = path.join(path.dirname(file), 'unsettled-helper.py')
  fs.writeFileSync(helper, `import json,os,sys\nsys.path.insert(0,${JSON.stringify(root)})\nfrom server.backend_cleanup import BackendCleanup\nproof=BackendCleanup(json.loads(os.environ['AMADEUS_BACKEND_CLEANUP_LAUNCH']))\nassert proof.failure is None, proof.failure\n${outcome === 'crash' ? 'os._exit(17)' : 'proof.record_runtime_cleanup(providers_settled=True,work_settled=False,teardown_succeeded=True)\nassert not proof.finish()'}\n`)
  const result = spawnSync(python, ['-B', helper], { cwd: root, env: { ...process.env, ...environment }, encoding: 'utf8', timeout: 10000, windowsHide: true })
  assert.equal(result.status, outcome === 'crash' ? 17 : 0, result.stdout + result.stderr)
  const receipt = path.join(`${file}.receipts`, `${launch.owner.id}.json`)
  let clean = false
  for (let attempt = 0; attempt < 40 && !clean; attempt++) {
    clean = await verifyPreviousBackendCleanup(file)
    if (!clean) await new Promise(resolve => setTimeout(resolve, 25))
  }
  assert.equal(clean, true)
  assert.equal(JSON.parse(fs.readFileSync(receipt, 'utf8')).state, 'exited')
  assert.equal(fs.existsSync(path.join(`${file}.receipts`, `${launch.owner.id}.approval.json`)), false)
})

for (const scenario of ['failed-request', 'graceful-timeout', 'ancestor-junction']) {
  test(`native owned Job stop preserves its observer: ${scenario}`, {skip: process.platform !== 'win32', timeout: 60000}, async t => {
    let file = cleanupFixture(t)
    let link
    if (scenario === 'ancestor-junction') {
      const base = path.dirname(file)
      const target = path.join(base, 'relocated-profile')
      fs.mkdirSync(path.join(target, 'user-data'), {recursive: true})
      link = path.join(base, 'profile-link')
      fs.symlinkSync(target, link, 'junction')
      file = path.join(link, 'user-data', 'backend-cleanup.json')
    }
    const root = path.resolve('..'), python = existingPython(root)
    let proc, unrelated
    try {
      const {environment} = await prepareBackendCleanup(file, {...launchIdentity, projectRoot: root, pythonCommand: python})
      const rawLaunch = environment.AMADEUS_BACKEND_CLEANUP_LAUNCH
      const launch = JSON.parse(rawLaunch)
      const helper = path.join(path.dirname(file), 'hung-backend.py')
      fs.writeFileSync(helper, `import json,os,subprocess,sys,time\nsys.path.insert(0,${JSON.stringify(root)})\nfrom server.backend_cleanup import begin_from_environment\nproof=begin_from_environment()\nassert proof and proof.failure is None, proof.failure if proof else 'no launch'\nchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nassert not proof.job.only_backend_remains()\nprint(json.dumps({'pid':os.getpid(),'child':child.pid}),flush=True)\ntime.sleep(60)\n`)
      const env = {...process.env, ...environment}
      delete env.AMADEUS_BACKEND_INSTANCE_NONCE
      proc = spawn(python, ['-B', helper], {cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']})
      let errors = ''
      proc.stderr.on('data', data => {errors += data})
      const output = await Promise.race([
        once(proc.stdout, 'data'),
        once(proc, 'exit').then(() => {throw Error(`backend exited before readiness: ${errors}`)}),
      ])
      assert.ok(JSON.parse(String(output[0])).child > 0)
      assert.equal(await verifyPreviousBackendCleanup(file), false, 'live Job must fence maintenance')
      unrelated = spawn(python, ['-c', 'import time; time.sleep(60)'], {windowsHide: true, stdio: 'ignore'})
      await once(unrelated, 'spawn')
      // A different creation identity must never authorize Job termination.
      const trackingFile = path.join(`${file}.receipts`, `${launch.owner.id}.tracking.json`)
      const bytes = fs.readFileSync(trackingFile)
      const tracking = JSON.parse(bytes)
      tracking.process.created = String(BigInt(tracking.process.created) + 1n)
      fs.writeFileSync(trackingFile, JSON.stringify(tracking))
      const denied = spawnSync(python, ['-I', '-B', path.join(root, 'server/backend_cleanup.py'), '--terminate', rawLaunch], {windowsHide: true, encoding: 'utf8', timeout: 10000})
      assert.notEqual(denied.status, 0)
      assert.match(denied.stderr, /does not match/)
      assert.equal(proc.exitCode, null)
      fs.writeFileSync(trackingFile, bytes)
      assert.equal(await stopOwnedBackend(proc, async () => scenario === 'graceful-timeout', {
        cleanupLaunch: rawLaunch, verifyCleanup: () => verifyPreviousBackendCleanup(file),
      }), false, 'termination itself must not grant maintenance authority')
      assert.notEqual(proc.exitCode ?? proc.signalCode, null)
      let clean = false
      for (let i = 0; i < 40 && !clean; i++) {
        clean = await verifyPreviousBackendCleanup(file)
        if (!clean) await new Promise(resolve => setTimeout(resolve, 50))
      }
      assert.equal(clean, true, 'observer must survive Electron escalation and certify the drained Job')
      assert.equal(JSON.parse(fs.readFileSync(path.join(`${file}.receipts`, `${launch.owner.id}.json`))).state, 'exited')
      assert.equal(unrelated.exitCode, null, 'unrelated process must remain alive')
    } finally {
      if (proc && proc.exitCode === null && proc.signalCode === null) {
        await forceStopBackendTree(proc)
        await waitForProcessExit(proc, 5000)
      }
      if (unrelated) {unrelated.kill(); await waitForProcessExit(unrelated, 5000)}
      if (link) fs.unlinkSync(link)
    }
  })
}
