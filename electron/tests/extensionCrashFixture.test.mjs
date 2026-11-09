import assert from 'node:assert/strict'
import fs from 'node:fs'
import {EventEmitter} from 'node:events'
import test from 'node:test'

const source = fs.readFileSync(new URL('./fixtures/extensionRelease.cjs', import.meta.url), 'utf8')
const capture = new Function(source + '\nreturn captureFixtureBackend')()

test('crash injection targets only the fixture backend ChildProcess and refreshes its ESM binding', async () => {
  const children = [], nonce = 'synthetic-fixture-nonce'
  const processes = {spawn: () => {
    const child = Object.assign(new EventEmitter(), {exitCode: null, signalCode: null, killed: false})
    child.kill = () => {child.killed = true; queueMicrotask(() => {child.exitCode = 1; child.emit('exit', 1)}); return true}
    children.push(child)
    return child
  }}
  let synced = 0
  const crash = capture(processes, () => synced++, nonce)
  assert.equal(synced, 1)
  processes.spawn('python', ['-m', 'server.app'], {env: {AMADEUS_BACKEND_INSTANCE_NONCE: 'other-nonce'}})
  processes.spawn('python', ['helper.py'], {env: {AMADEUS_BACKEND_INSTANCE_NONCE: nonce}})
  await assert.rejects(crash(), /No live owned/)
  assert.ok(children.every(child => !child.killed))
  const first = processes.spawn('python', ['-m', 'server.app', '--port', '17777'], {env: {AMADEUS_BACKEND_INSTANCE_NONCE: nonce}})
  assert.equal(await crash(), true)
  assert.equal(first.killed, true)
  assert.equal(first.listenerCount('exit') + first.listenerCount('error'), 0)
  await assert.rejects(crash(), /No live owned/)
  const next = processes.spawn('python', ['-m', 'server.app'], {env: {AMADEUS_BACKEND_INSTANCE_NONCE: nonce}})
  assert.equal(await crash(), true)
  assert.equal(next.killed, true)
  assert.ok(children.slice(0, 2).every(child => !child.killed))
})
