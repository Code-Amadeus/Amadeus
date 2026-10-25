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

test('record-only recovery does not require file-cleanup authority and cannot grant it', async () => {
  const boundary = new ExtensionUpdateBoundary(async () => false, async () => true)
  await boundary.runRecovery(async () => {
    boundary.assertRecoveryHeld()
    assert.throws(boundary.assertHeld, /stopped-backend/)
    assert.throws(() => boundary.assertBackendMayStart(), /blocked/)
    await assert.rejects(boundary.run(async () => assert.fail('install')), /Another/)
  })
  await assert.rejects(boundary.run(async () => assert.fail('install')), /active or unconfirmed/)
  boundary.assertBackendMayStart()
  assert.throws(boundary.assertRecoveryHeld, /recovery boundary/)
  const running = new ExtensionUpdateBoundary(async () => false, async () => false)
  await assert.rejects(running.runRecovery(async () => assert.fail('live recovery')), /backend is still active/)
})
