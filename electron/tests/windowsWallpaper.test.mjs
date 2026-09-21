import assert from 'node:assert/strict'
import test from 'node:test'
import { managesWindowsWallpaper, WindowsWallpaperSession } from '../src/main/windowsWallpaper.ts'

test('managed host is Windows-only; external hosts remain selectable', () => {
  assert.equal(managesWindowsWallpaper('win32', {}), true)
  assert.equal(managesWindowsWallpaper('win32', { AMADEUS_WALLPAPER_HOST: 'external' }), false)
  for (const platform of ['darwin', 'linux']) {
    assert.equal(managesWindowsWallpaper(platform, {}), false)
    assert.equal(managesWindowsWallpaper(platform, { AMADEUS_WALLPAPER_HOST: 'lively' }), false)
  }
})

function fixture() {
  const events = []
  let mount
  const session = new WindowsWallpaperSession({
    prepare: async () => { events.push('prepare') },
    launch: url => {
      events.push(`launch ${url}`)
      let restored
      return {
        process: { stdin: { end: () => { events.push('restore'); restored() } } },
        ready: new Promise(resolve => { mount = resolve }),
        done: new Promise(resolve => { restored = resolve }),
      }
    },
  })
  return { session, events, mounted: () => mount() }
}

test('ready event and start response share one mount and one before-image', async () => {
  const f = fixture()
  const first = f.session.start('one')
  assert.equal(first, f.session.start('one'))
  await new Promise(resolve => setImmediate(resolve))
  f.mounted()
  await first
  await f.session.start('one')
  await f.session.stop()
  await f.session.stop()
  assert.deepEqual(f.events, ['prepare', 'launch one', 'restore'])
})

test('quit during mounting waits for the mount and restores exactly once', async () => {
  const f = fixture()
  const first = f.session.start('one')
  const stopped = f.session.stop()
  await new Promise(resolve => setImmediate(resolve))
  f.mounted()
  await Promise.all([first, stopped])
  assert.deepEqual(f.events, ['prepare', 'launch one', 'restore'])
})

test('a changed bridge restores the previous session before mounting again', async () => {
  const f = fixture()
  const first = f.session.start('one')
  await new Promise(resolve => setImmediate(resolve))
  f.mounted()
  await first
  const second = f.session.start('two')
  await new Promise(resolve => setImmediate(resolve))
  f.mounted()
  await second
  await f.session.stop()
  assert.deepEqual(f.events, ['prepare', 'launch one', 'restore', 'prepare', 'launch two', 'restore'])
})

test('mount failure waits for cleanup and remains visible to the caller', async () => {
  let restored = false
  let complete
  const done = new Promise(resolve => { complete = resolve })
  const session = new WindowsWallpaperSession({
    prepare: async () => {},
    launch: () => ({
      process: { stdin: { end: () => { restored = true; complete() } } },
      ready: Promise.reject(new Error('mount failed')),
      done,
    }),
  })
  await assert.rejects(session.start('one'), /mount failed/)
  assert.equal(restored, true)
})

test('an unexpected helper exit invalidates the active session and reports the failure', async () => {
  let fail
  const errors = []
  const session = new WindowsWallpaperSession({
    prepare: async () => {},
    launch: () => ({
      process: { stdin: { end: () => {} } },
      ready: Promise.resolve(),
      done: new Promise((_resolve, reject) => { fail = reject }),
    }),
    exited: error => errors.push(error),
  })
  await session.start('one')
  fail(new Error('host gone'))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(errors[0].message, 'host gone')
  await session.stop()
})
