import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const { forceStopBackendTree } = loadTypeScript(new URL('../src/main/backendProcessTree.ts', import.meta.url))

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
