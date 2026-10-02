import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { resolveStartupMode } from '../src/main/startupMode.ts'

const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('index.ts', source, ts.ScriptTarget.ES2022, true)
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const startup = ast.statements.find(node => ts.isVariableStatement(node)
  && node.declarationList.declarations.some(item => item.name?.getText(ast) === 'STARTUP_MODE'))
const startupDeclaration = startup.declarationList.declarations.find(item => item.name?.getText(ast) === 'STARTUP_MODE')

test('GUI startup preference changes take effect next launch, not while a window is open', () => {
  for (const initial of ['window', 'wallpaper']) {
    let saved = initial
    let reads = 0
    const startupMode = new Function('desktopSettings', 'resolveStartupMode', 'process',
      'const STARTUP_MODE = ' + compile(startupDeclaration.initializer.getText(ast)) + ';\nreturn STARTUP_MODE;')(
      { snapshot: () => { reads += 1; return { values: { AMADEUS_WINDOWS_STARTUP_MODE: saved } } } },
      resolveStartupMode, { argv: ['electron', '.'], env: {}, platform: 'win32' },
    )
    assert.equal(startupMode, initial)
    saved = initial === 'wallpaper' ? 'window' : 'wallpaper'
    // The launch resolves its surfaces once: later preference edits cannot
    // change the window this process already owns.
    assert.equal(startupMode, initial)
    assert.equal(reads, 1)
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
