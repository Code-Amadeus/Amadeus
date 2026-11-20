import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

test('Steam startup remains pending after 45 seconds and can be cancelled', async () => {
  const source = readFileSync(new URL('../src/renderer/hooks/useBackend.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const timers = new Map()
  let now = 0
  let nextTimer = 0
  let nextRequest = 0
  let socket

  class WebSocketStub {
    static OPEN = 1
    readyState = 1
    sent = []
    constructor() { socket = this }
    send(raw) { this.sent.push(JSON.parse(raw)) }
  }

  const module = { exports: {} }
  vm.runInNewContext(compiled, {
    module,
    exports: module.exports,
    require: name => {
      assert.equal(name, 'react')
      return {
        useRef: value => ({ current: value }),
        useState: value => [value, () => {}],
        useCallback: fn => fn,
        useEffect: fn => { fn() },
      }
    },
    WebSocket: WebSocketStub,
    window: {},
    crypto: { randomUUID: () => `request-${++nextRequest}` },
    performance: { now: () => now },
    console,
    setTimeout: (fn, delay) => {
      const id = ++nextTimer
      timers.set(id, { fn, at: now + delay })
      return id
    },
    clearTimeout: id => timers.delete(id),
  })

  const { send } = module.exports.useBackend()
  const advance = milliseconds => {
    now += milliseconds
    for (const [id, timer] of [...timers]) {
      if (timer.at <= now) {
        timers.delete(id)
        timer.fn()
      }
    }
  }
  const respond = (request, params) => socket.onmessage({
    data: JSON.stringify({ type: 'res', id: request.id, method: '', params }),
  })

  let startSettled = false
  const start = send('vn.launch.start', { profileId: 'steam-game' })
    .finally(() => { startSettled = true })
  advance(45_000)
  await Promise.resolve()
  assert.equal(startSettled, false)

  const stop = send('vn.launch.stop')
  assert.deepEqual(socket.sent.map(request => request.method), ['vn.launch.start', 'vn.launch.stop'])
  respond(socket.sent[1], { status: 'idle' })
  assert.equal((await stop).status, 'idle')
  respond(socket.sent[0], { status: 'idle' })
  assert.equal((await start).status, 'idle')
})
