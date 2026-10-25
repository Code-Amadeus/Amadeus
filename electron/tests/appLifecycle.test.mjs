import assert from 'node:assert/strict'
import test from 'node:test'

import { ApplicationLifecycle } from '../src/main/appLifecycle.ts'

test('ordinary wallpaper close hides the window without becoming quit intent', () => {
  const lifecycle = new ApplicationLifecycle()
  assert.equal(lifecycle.shouldHideWallpaperWindow(true), true)
  assert.equal(lifecycle.shouldHideWallpaperWindow(true), true)
})

test('explicit quit closes wallpaper windows when the backend is absent', () => {
  const lifecycle = new ApplicationLifecycle()
  assert.equal(lifecycle.beginQuit(false), false)
  assert.equal(lifecycle.shouldHideWallpaperWindow(true), false)
})

test('explicit quit stops a live backend once and then permits window closure', () => {
  const lifecycle = new ApplicationLifecycle()
  assert.equal(lifecycle.isQuitting, false)
  assert.equal(lifecycle.beginQuit(true), true)
  assert.equal(lifecycle.isQuitting, true)
  assert.equal(lifecycle.shouldHideWallpaperWindow(true), false)
  assert.equal(lifecycle.beginQuit(true), false)
})

test('a second launch during backend startup defers restoration without creating a window', () => {
  const startup = new ApplicationLifecycle()
  const actions = []

  actions.push(startup.requestMainWindow(false))
  actions.push('startup-create')
  if (startup.completeStartup()) actions.push('restore')

  assert.deepEqual(actions, ['defer', 'startup-create', 'restore'])
})

test('a second launch after startup creates or restores exactly one main window', () => {
  const startup = new ApplicationLifecycle()
  assert.equal(startup.completeStartup(), false)
  assert.equal(startup.requestMainWindow(false), 'create')
  assert.equal(startup.requestMainWindow(true), 'restore')
})

for (const fail of [false, true]) {
  test(`quit settles cleanup and requests exit exactly once (failure=${fail})`, async () => {
    const lifecycle = new ApplicationLifecycle()
    const calls = []
    const failure = Error('synthetic backend cleanup failure')
    await lifecycle.finishQuit(async () => {
      calls.push('cleanup')
      if (fail) throw failure
    }, error => { assert.equal(error, failure); calls.push('reported') }, () => calls.push('quit'))
    assert.deepEqual(calls, fail ? ['cleanup', 'reported', 'quit'] : ['cleanup', 'quit'])
  })
}

test('a cancelled Windows shutdown query leaves the app usable; confirmed session end commits quit intent', async () => {
  const lifecycle = new ApplicationLifecycle()
  let release, calls = 0
  const cleanup = () => { calls++; return new Promise(resolve => { release = resolve }) }
  const query = lifecycle.handleSessionEnd(false, cleanup, assert.fail)
  await Promise.resolve()
  assert.equal(lifecycle.isQuitting, false)
  assert.equal(lifecycle.shouldHideWallpaperWindow(true), true)
  const ended = lifecycle.handleSessionEnd(true, cleanup, assert.fail)
  assert.equal(lifecycle.isQuitting, true)
  assert.equal(lifecycle.shouldHideWallpaperWindow(true), false)
  assert.equal(calls, 1)
  release()
  await Promise.all([query, ended])
})

test('session-end cleanup failure is observable without creating permanent quit intent for a cancelled query', async () => {
  const lifecycle = new ApplicationLifecycle(), errors = []
  await lifecycle.handleSessionEnd(false, async () => { throw Error('shutdown failed') }, error => errors.push(error.message))
  assert.deepEqual(errors, ['shutdown failed'])
  assert.equal(lifecycle.isQuitting, false)
  let retried = false
  await lifecycle.handleSessionEnd(false, async () => { retried = true }, assert.fail)
  assert.equal(retried, true)
})
