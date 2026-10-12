import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const { forceStopBackendTree, stopOwnedBackend, waitForProcessExit } = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url))

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

test('graceful owned shutdown confirms lifetime without forced termination', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => true, {
    waitForExit: async () => { proc.exit(); return true },
    forceStop: async () => { assert.fail('graceful stop must not force the tree') },
  }), true)
})

test('forced Windows shutdown confirms children only after backend exit', async () => {
  const proc = processFixture()
  assert.equal(await stopOwnedBackend(proc, async () => false, { platform: 'win32',
    forceStop: async owned => { assert.equal(owned, proc); proc.exit() },
    waitForExit: async () => true,
  }), true)
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
  }), true)
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
