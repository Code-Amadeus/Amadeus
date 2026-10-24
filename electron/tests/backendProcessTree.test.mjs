import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const { forceStopBackendTree, stopOwnedBackend, waitForProcessExit, prepareBackendCleanup,
  verifyPreviousBackendCleanup, markBackendCleanupUnconfirmed } = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url))

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

function processFixture() {
  const proc = new EventEmitter()
  Object.assign(proc, { pid: 12345, exitCode: null, signalCode: null })
  proc.exit = () => { proc.exitCode = 0; proc.emit('exit', 0) }
  return proc
}

test('graceful owned shutdown requires the provider and process-tree receipt', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => true, {
    waitForExit: async () => { proc.exit(); return true },
    forceStop: async () => { assert.fail('graceful stop must not force the tree') },
    verifyCleanup: async () => true,
  }), true)
  const noReceipt = processFixture()
  assert.equal(await stopOwnedBackend(noReceipt, async () => true, {
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
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-cleanup-'))
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

test('registration survives crash and a later clean launch cannot erase it', async t => {
  const file = cleanupFixture(t)
  await prepareBackendCleanup(file, launchIdentity)
  const second = await prepareBackendCleanup(file, launchIdentity)
  writeReceipt(file, second.environment)
  assert.equal(JSON.parse(fs.readFileSync(file)).owners.length, 2)
  assert.equal(await verifyPreviousBackendCleanup(file, goneAndDrained), false)
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
    schema: 'amadeus.backend-cleanup.v1', unknown: true, owners: [],
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

test('real Windows tracking Job detects a child surviving its parent without killing it', { skip: process.platform !== 'win32' }, async t => {
  const file = cleanupFixture(t)
  const root = path.resolve('..')
  const python = path.join(root, '.venv', 'Scripts', 'python.exe')
  assert.equal(fs.existsSync(python), true, 'use the existing project Python, never install dependencies')
  const { environment } = await prepareBackendCleanup(file, { ...launchIdentity, projectRoot: root, pythonCommand: python })
  const release = path.join(path.dirname(file), 'release-child')
  const started = path.join(path.dirname(file), 'child-started')
  const helper = path.join(path.dirname(file), 'job-helper.py')
  const childSource = `import pathlib,time; p=pathlib.Path(${JSON.stringify(release)}); pathlib.Path(${JSON.stringify(started)}).write_text('started'); end=time.monotonic()+20\nwhile not p.exists() and time.monotonic()<end: time.sleep(.02)`
  fs.writeFileSync(helper, `import json,os,subprocess,sys,time\nfrom pathlib import Path\nsys.path.insert(0,${JSON.stringify(root)})\nfrom server.backend_cleanup import BackendCleanup\nlaunch=json.loads(os.environ['AMADEUS_BACKEND_CLEANUP_LAUNCH'])\nproof=BackendCleanup(launch)\nif proof.failure:\n print(json.dumps({'failure':proof.failure}));sys.exit(3)\nproof.record_runtime_cleanup(providers_settled=True,work_settled=True,teardown_succeeded=True)\nassert proof.finish()\n# Model a late finalizer child after the backend's last observation.\nchild=subprocess.Popen([sys.executable,'-c',${JSON.stringify(childSource)}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nend=time.monotonic()+5\nwhile not Path(${JSON.stringify(started)}).exists() and time.monotonic()<end: time.sleep(.02)\nassert Path(${JSON.stringify(started)}).exists(), f'child did not start: {child.poll()}'\nassert not proof.job.only_backend_remains(), 'Job must include the live child'\nprint(json.dumps({'child':child.pid,'root':os.getpid()}))\n`)
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

test('a real backend exit without cleanup approval leaves diagnostic pending evidence', { skip: process.platform !== 'win32' }, async t => {
  const file = cleanupFixture(t)
  const root = path.resolve('..')
  const python = path.join(root, '.venv', 'Scripts', 'python.exe')
  const { environment } = await prepareBackendCleanup(file, { ...launchIdentity, projectRoot: root, pythonCommand: python })
  const launch = JSON.parse(environment.AMADEUS_BACKEND_CLEANUP_LAUNCH)
  const helper = path.join(path.dirname(file), 'unsettled-helper.py')
  fs.writeFileSync(helper, `import json,os,sys\nsys.path.insert(0,${JSON.stringify(root)})\nfrom server.backend_cleanup import BackendCleanup\nproof=BackendCleanup(json.loads(os.environ['AMADEUS_BACKEND_CLEANUP_LAUNCH']))\nassert proof.failure is None, proof.failure\n# Exit without record_runtime_cleanup/finish, as a failed teardown would.\n`)
  const result = spawnSync(python, ['-B', helper], { cwd: root, env: { ...process.env, ...environment }, encoding: 'utf8', timeout: 10000, windowsHide: true })
  assert.equal(result.status, 0, result.stdout + result.stderr)
  const failed = path.join(`${file}.receipts`, `${launch.owner.id}.failed.json`)
  for (let attempt = 0; attempt < 40 && !fs.existsSync(failed); attempt++) await new Promise(resolve => setTimeout(resolve, 25))
  assert.equal(JSON.parse(fs.readFileSync(failed, 'utf8')).reason, 'backend_cleanup_approval_missing')
  assert.equal(await verifyPreviousBackendCleanup(file), false)
  assert.equal(fs.existsSync(path.join(`${file}.receipts`, `${launch.owner.id}.json`)), false)
})
