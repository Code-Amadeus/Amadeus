import assert from 'node:assert/strict'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
const { ExtensionUpdateBoundary } = loadTypeScript(new URL('../src/main/extensionUpdates.ts', import.meta.url))

test('maintenance blocks new backend starts during both idle proof and the whole install', async () => {
  let prove, finish
  const boundary = new ExtensionUpdateBoundary(() => new Promise(resolve => { prove = resolve }))
  const run = boundary.run(async () => {
    boundary.assertHeld()
    assert.throws(() => boundary.assertBackendMayStart(), /blocked/)
    await new Promise(resolve => { finish = resolve })
  })
  assert.throws(() => boundary.assertBackendMayStart(), /blocked/)
  await assert.rejects(boundary.run(async () => {}), /Another/)
  prove(true)
  await Promise.resolve()
  assert.throws(() => boundary.assertBackendMayStart(), /blocked/)
  finish(); await run
  boundary.assertBackendMayStart()
  assert.throws(boundary.assertHeld, /stopped-backend/)
})

test('active or unknown backend never executes an install, and failures release the start fence', async () => {
  let executed = false
  const blocked = new ExtensionUpdateBoundary(async () => false)
  await assert.rejects(blocked.run(async () => { executed = true }), /active or unconfirmed/)
  assert.equal(executed, false)
  blocked.assertBackendMayStart()
  const idle = new ExtensionUpdateBoundary(async () => true)
  await assert.rejects(idle.run(async () => { throw new Error('prepare failed') }), /prepare failed/)
  idle.assertBackendMayStart()
})
