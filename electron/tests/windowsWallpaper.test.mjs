import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { managesWindowsWallpaper, recoverWindowsWallpaperHostExit, stopBackendWallpaperAfterHostExit, stopWallpaperForRenderer, windowsWallpaperDependencies, WindowsWallpaperSession } from '../src/main/windowsWallpaper.ts'

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

test('same-URL start retries preparation after the external failure is repaired', async () => {
  let attempts = 0
  const session = new WindowsWallpaperSession({
    prepare: async () => { if (++attempts === 1) throw new Error('lively missing') },
    launch: () => ({ process: { stdin: { end: () => {} } }, ready: Promise.resolve(), done: new Promise(() => {}) }),
  })
  await assert.rejects(session.start('same'), /lively missing/)
  await session.start('same')
  assert.equal(attempts, 2)
})

test('same-URL start retries a failed mount without requiring a separate stop', async () => {
  let launches = 0
  const session = new WindowsWallpaperSession({
    prepare: async () => {},
    launch: () => {
      const failed = ++launches === 1
      return {
        process: { stdin: { end: () => {} } },
        ready: failed ? Promise.reject(new Error('mount failed')) : Promise.resolve(),
        done: failed ? Promise.resolve() : new Promise(() => {}),
      }
    },
  })
  await assert.rejects(session.start('same'), /mount failed/)
  await session.start('same')
  assert.equal(launches, 2)
})

test('failed renderer close resolves false, reports the error, and permits another attempt', async () => {
  let attempts = 0
  const errors = []
  const session = { stop: async () => { if (++attempts === 1) throw new Error('restoration failed') } }
  assert.equal(await stopWallpaperForRenderer(session, error => errors.push(error)), false)
  assert.equal(errors[0].message, 'restoration failed')
  assert.equal(await stopWallpaperForRenderer(session, error => errors.push(error)), true)
  assert.equal(await stopWallpaperForRenderer(null, () => assert.fail('non-Windows stop reported error')), true)
})

test('real setup process is rerun after a previous setup failure', { skip: process.platform !== 'win32' }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'amadeus-setup-retry-'))
  try {
    const scripts = path.join(root, 'scripts')
    await fs.mkdir(scripts)
    await fs.writeFile(path.join(scripts, 'setup_windows_wallpaper.ps1'), `
$marker = Join-Path $PSScriptRoot 'attempted'
if (Test-Path -LiteralPath $marker) { exit 0 }
Set-Content -LiteralPath $marker -Value 'attempted'
Write-Error 'fixture installation unavailable'
exit 1
`)
    const dependencies = windowsWallpaperDependencies(root, '', false)
    await assert.rejects(dependencies.prepare())
    await dependencies.prepare()
  } finally {
    await fs.rm(root, { recursive: true, force: true })
  }
})

test('slow start and stop retain visible lifecycle status until the operation completes', async () => {
  const statuses = []
  let mounted, restored
  const session = new WindowsWallpaperSession({
    prepare: async () => {},
    launch: () => ({
      process: { stdin: { end: () => {} } },
      ready: new Promise(resolve => { mounted = resolve }),
      done: new Promise(resolve => { restored = resolve }),
    }),
    status: status => statuses.push(status),
  })
  assert.equal(session.active, false)
  const started = session.start('one')
  assert.equal(session.active, true)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(statuses.at(-1), 'mounting')
  mounted()
  await started
  assert.equal(statuses.at(-1), 'active')
  const stopped = session.stop()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(statuses.at(-1), 'restoring')
  restored()
  await stopped
  assert.equal(statuses.at(-1), 'idle')
  assert.equal(session.active, false)
})


for (const outcome of ['acknowledged', 'rejected', 'disconnected']) {
  test(`host exit ends backend wallpaper ownership (${outcome})`, async () => {
    const calls = []
    let ended
    const session = new WindowsWallpaperSession({
      prepare: async () => {},
      launch: () => ({process: {stdin: {end() {}}}, ready: Promise.resolve(),
        done: new Promise((_resolve, reject) => { ended = reject })}),
      exited: () => stopBackendWallpaperAfterHostExit(async () => {
        calls.push('wallpaper.stop')
        if (outcome === 'disconnected') throw new Error('connection lost')
        return outcome === 'acknowledged'
      }, async () => { calls.push('owned-backend.stop') }),
    })
    await session.start('one')
    ended(new Error('helper killed'))
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(calls, outcome === 'acknowledged'
      ? ['wallpaper.stop'] : ['wallpaper.stop', 'owned-backend.stop'])
  })
}


function recoveryActions(overrides = {}) {
  const messages = []
  const calls = []
  return { messages, calls, actions: {
    closeSurface: () => { calls.push('close') },
    showMainWindow: () => { calls.push('show') },
    requestStop: async () => { calls.push('wallpaper.stop'); return true },
    stopBackend: async () => { calls.push('backend.stop') },
    reportError: message => { messages.push(message) },
    ...overrides,
  } }
}

test('backend fallback always explains restart even when the helper exited without an error', async () => {
  const f = recoveryActions({ requestStop: async () => false })
  await recoverWindowsWallpaperHostExit(undefined, f.actions)
  assert(f.calls.includes('backend.stop'))
  assert.equal(f.messages.length, 1)
  assert.match(f.messages[0], /stopped its backend/)
  assert.match(f.messages[0], /Restart Amadeus/)
  assert.match(f.messages[0], /reconnecting alone will not restart/)
})

test('acknowledged wallpaper cleanup keeps the backend and only reports the original failure', async () => {
  const f = recoveryActions()
  await recoverWindowsWallpaperHostExit(new Error('Lively is missing'), f.actions)
  assert(!f.calls.includes('backend.stop'))
  assert.match(f.messages[0], /Lively is missing/)
  assert.doesNotMatch(f.messages[0], /stopped its backend/)
})

test('cleanup failure preserves the initial error and does not claim shutdown succeeded', async () => {
  const f = recoveryActions({ requestStop: async () => false,
    stopBackend: async () => { throw new Error('shutdown failed') } })
  assert.equal(await recoverWindowsWallpaperHostExit(new Error('Lively is missing'), f.actions), false)
  assert.match(f.messages[0], /Lively is missing/)
  assert.match(f.messages[0], /shutdown failed/)
  assert.match(f.messages[0], /shutdown could not be confirmed/)
  assert.doesNotMatch(f.messages[0], /stopped its backend/)
})

for (const failedAction of ['closeSurface', 'showMainWindow']) {
  test(`${failedAction} failure cannot skip voice cleanup or reject the exit notification`, async () => {
    const f = recoveryActions({ [failedAction]: () => { throw new Error('window destroyed') } })
    await assert.doesNotReject(recoverWindowsWallpaperHostExit(new Error('helper lost'), f.actions))
    assert(f.calls.includes('wallpaper.stop'))
    assert.match(f.messages[0], /helper lost/)
    assert.match(f.messages[0], /window destroyed/)
  })
}

test('a failed recovery dialog does not create an unhandled rejection', async t => {
  const errors = []
  t.mock.method(console, 'error', (...args) => errors.push(args))
  const f = recoveryActions({ reportError: () => { throw new Error('dialog unavailable') } })
  await assert.doesNotReject(recoverWindowsWallpaperHostExit(new Error('helper lost'), f.actions))
  assert(f.calls.includes('wallpaper.stop'))
  assert.match(errors[0].join(' '), /helper lost/)
  assert.match(errors[0].join(' '), /dialog unavailable/)
})
