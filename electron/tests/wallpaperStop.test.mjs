import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { stopElectronSliceHost } from '../src/renderer/wallpaperSlice.ts'
import { recoverWindowsWallpaperHostExit, stopWallpaperForRenderer, WindowsWallpaperSession } from '../src/main/windowsWallpaper.ts'

// Exercise the actual trusted IPC handler and renderer helper together, using
// the same AST isolation as renderSamplingBridge.test.mjs (no native desktop).
const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('index.ts', source, ts.ScriptTarget.ES2022, true)
const call = ast.statements.find(statement => ts.isExpressionStatement(statement)
  && ts.isCallExpression(statement.expression)
  && statement.expression.expression.getText(ast) === 'ipcMain.handle'
  && statement.expression.arguments[0]?.text === 'electron-slice.close').expression
const compiled = ts.transpileModule(`const handler = ${call.arguments[1].getText(ast)}`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

for (const scenario of ['acknowledged', 'rpc-failed', 'both-transports-failed', 'shutdown-failed', 'darwin', 'linux']) {
  test(`explicit wallpaper stop preserves ownership through ${scenario}`, async () => {
    const previous = globalThis.window
    const events = [], notices = []
    let helperDone
    const session = new WindowsWallpaperSession({
      prepare: async () => {},
      launch: () => ({ process: { stdin: { end() { events.push('restore'); helperDone() } } },
        ready: Promise.resolve(), done: new Promise(resolve => { helperDone = resolve }) }),
      exited: () => assert.fail('explicit stop must not rely on the unexpected-exit callback'),
    })
    await session.start('scene')
    const platform = ['darwin', 'linux'].includes(scenario) ? scenario : 'win32'
    const closeSurface = () => { events.push('close') }
    const recover = error => recoverWindowsWallpaperHostExit(error, {
      closeSurface, showMainWindow: () => {},
      requestStop: async () => { events.push('http-stop'); return scenario === 'rpc-failed' },
      stopBackend: async () => {
        events.push('backend-stop')
        if (scenario === 'shutdown-failed') throw new Error('cannot confirm shutdown')
      },
      reportError: message => { notices.push(message) },
    })
    const close = new Function('isMainRenderer', 'process', 'handleWindowsWallpaperHostExit',
      'closeElectronSliceWindow', 'stopWallpaperForRenderer', 'windowsWallpaper', 'mainWindow', 'dialog',
      compiled + '; return handler;')(
      sender => sender === 'main', { platform }, recover, closeSurface,
      stopWallpaperForRenderer, session, null, { showErrorBox: (_title, message) => notices.push(message) },
    )
    globalThis.window = { amadeus: { closeElectronSlice: error => close({ sender: 'main' }, error) } }
    try {
      const stopped = await stopElectronSliceHost(async method => {
        assert.equal(method, 'wallpaper.stop')
        events.push('rpc-stop')
        if (scenario !== 'acknowledged') throw new Error('RPC timed out')
        return { status: 'stopped' }
      })
      assert.equal(stopped, scenario !== 'shutdown-failed')
      assert.equal(events.filter(event => event === 'restore').length, 1)
      const fallbackExpected = platform === 'win32' && scenario !== 'acknowledged'
      assert.equal(events.includes('http-stop'), fallbackExpected)
      assert.equal(events.includes('backend-stop'), ['both-transports-failed', 'shutdown-failed'].includes(scenario))
      if (fallbackExpected) assert.match(notices[0], /RPC timed out/)
      if (scenario === 'both-transports-failed') assert.match(notices[0], /Restart Amadeus/)
      if (scenario === 'shutdown-failed') assert.match(notices[0], /shutdown could not be confirmed/)
      // The backend's exited notification closes the surface without sending
      // another stop RPC and creating a wallpaper.exited feedback loop.
      const stopCount = events.filter(event => event === 'http-stop').length
      await close({ sender: 'main' })
      assert.equal(events.filter(event => event === 'http-stop').length, stopCount)
      assert.equal(await close({ sender: 'untrusted' }, 'injected error'), false)
    } finally { globalThis.window = previous }
  })
}
