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
  assert.equal(lifecycle.beginQuit(true), true)
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
