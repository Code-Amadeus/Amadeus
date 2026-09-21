import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { isWallpaperStartup } from '../src/main/startupMode.ts'

const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('index.ts', source, ts.ScriptTarget.ES2022, true)
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const startup = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'wantsWallpaper')

test('GUI startup preference changes take effect next launch, not while a window is open', () => {
  for (const initial of ['window', 'wallpaper']) {
    let saved = initial
    const wantsWallpaper = new Function('desktopSettings', 'isWallpaperStartup', 'process',
      'let wallpaperStartup;\n' + compile(startup.getText(ast)) + '\nreturn wantsWallpaper;')(
      { snapshot: () => { return { values: { AMADEUS_WINDOWS_STARTUP_MODE: saved } } } },
      isWallpaperStartup, { argv: ['electron', '.'], env: {}, platform: 'win32' },
    )
    assert.equal(wantsWallpaper(), initial === 'wallpaper')
    saved = initial === 'wallpaper' ? 'window' : 'wallpaper'
    assert.equal(wantsWallpaper(), initial === 'wallpaper')
  }
})

const focus = ast.statements.find(node => ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
  && node.expression.expression.getText(ast) === 'ipcMain.handle'
  && node.expression.arguments[0]?.text === 'main-window.focus').expression.arguments[1]

test('the Windows Slice gear can restore the console without granting access to foreign renderers', () => {
  const calls = []
  const panel = { isMinimized: () => true, restore: () => calls.push('restore'),
    show: () => calls.push('show'), focus: () => calls.push('focus') }
  const sliceSender = {}, mainSender = {}
  const makeHandler = platform => new Function('process', 'isTrustedBackendRenderer', 'electronSliceWindow',
    'electronCanvasLifecycle', 'mainWindow', compile(`const handler = ${focus.getText(ast)}`) + '\nreturn handler;')(
    { platform }, sender => sender === mainSender, { webContents: sliceSender }, { window: null }, panel,
  )
  const handler = makeHandler('win32')
  assert.equal(handler({ sender: sliceSender }), true)
  assert.deepEqual(calls, ['restore', 'show', 'focus'])
  calls.length = 0
  assert.equal(handler({ sender: {} }), false)
  assert.deepEqual(calls, [])
  assert.equal(makeHandler('darwin')({ sender: sliceSender }), false)
  assert.equal(makeHandler('darwin')({ sender: mainSender }), true)
})
