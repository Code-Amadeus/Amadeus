import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const parent = fileURLToPath(new URL('../../build/extension-icon-tests/', import.meta.url))
fs.mkdirSync(parent, { recursive: true })
const png = fs.readFileSync(new URL('../../examples/extensions/desktop-theme/src/amadeus_example_theme/background.png', import.meta.url))
const limits = { label: 'Package icon', maxMiB: 1, maxDimension: 1024 }

test('initials preserve Unicode graphemes, uppercase Latin, and tolerate empty names', () => {
  const { extensionInitial } = loadTypeScript(new URL('../src/renderer/components/ExtensionIcon.tsx', import.meta.url))
  for (const [name, initial] of [[' fish audio', 'F'], ['语音工具', '语'], ['e\u0301clair', 'É'], ['👩‍💻 tools', '👩‍💻'], [' ', '?']]) {
    assert.equal(extensionInitial(name).normalize('NFC'), initial)
  }
})

test('package icons enforce containment, type, bytes and canvas bounds before decoding', t => {
  const root = fs.mkdtempSync(path.join(parent, 'bounds-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  let decodes = 0
  const { readExtensionImage } = loadTypeScript(new URL('../src/main/extensionImages.ts', import.meta.url), {
    electron: { nativeImage: { createFromBuffer: () => { decodes++; return { isEmpty: () => false, getSize: () => ({ width: 64, height: 64 }) } } } },
  })
  fs.writeFileSync(path.join(root, 'icon.png'), png)
  assert.equal(readExtensionImage(root, 'icon.png', limits).base64, png.toString('base64'))
  const count = decodes
  for (const relative of ['../icon.png', '/icon.png', 'https://example.invalid/icon.png', 'C:/icon.png']) {
    assert.throws(() => readExtensionImage(root, relative, limits), /relative path/)
  }
  const wide = Buffer.from(png); wide.writeUInt32BE(1025, 16)
  fs.writeFileSync(path.join(root, 'icon.png'), wide)
  assert.throws(() => readExtensionImage(root, 'icon.png', limits), /dimensions/)
  fs.writeFileSync(path.join(root, 'icon.png'), Buffer.alloc(1024 * 1024 + 1))
  assert.throws(() => readExtensionImage(root, 'icon.png', limits), /1 MiB/)
  fs.writeFileSync(path.join(root, 'icon.png'), '<svg/>')
  assert.throws(() => readExtensionImage(root, 'icon.png', limits), /matching bytes/)
  fs.mkdirSync(path.join(root, 'package')); fs.mkdirSync(path.join(root, 'outside'))
  fs.writeFileSync(path.join(root, 'outside/icon.png'), png)
  fs.symlinkSync(path.join(root, 'outside'), path.join(root, 'package/linked'), process.platform === 'win32' ? 'junction' : 'dir')
  assert.throws(() => readExtensionImage(path.join(root, 'package'), 'linked/icon.png', limits), /links/)
  assert.equal(decodes, count)
})

test('icon lookup uses the registered installation and follows updates/removal without exposing paths', t => {
  const root = fs.mkdtempSync(path.join(parent, 'lookup-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const { ExtensionInstaller } = loadTypeScript(new URL('../src/main/extensions.ts', import.meta.url), {
    electron: { nativeImage: { createFromBuffer: () => ({ isEmpty: () => false, getSize: () => ({ width: 64, height: 64 }) }) } },
  })
  let records = []
  const installer = new ExtensionInstaller(root, root, { extensionRecords: () => records }, {}, () => '', {})
  function record(installId, bytes) {
    const directory = path.join(root, installId, 'package/demo')
    fs.mkdirSync(directory, { recursive: true })
    fs.writeFileSync(path.join(directory, 'icon.png'), bytes)
    return { id: 'org.example.icon', installId, sitePackages: '', packageRoot: 'demo', manifest: { icon: 'icon.png' } }
  }
  const old = record('1'.repeat(32), png)
  records = [old]
  assert.equal(installer.icon(old.id), `data:image/png;base64,${png.toString('base64')}`)
  assert.equal(installer.icon('../demo/icon.png'), null)
  const updated = Buffer.concat([png, Buffer.from('new package')])
  records = [record('2'.repeat(32), updated)]
  assert.equal(installer.icon(old.id), `data:image/png;base64,${updated.toString('base64')}`)
  records[0].manifest.icon = undefined
  assert.equal(installer.icon(old.id), null)
  records[0].manifest.icon = 'missing.png'
  assert.equal(installer.icon(old.id), null)
  records = []
  assert.equal(installer.icon(old.id), null)
})

test('icon IPC admits only a trusted renderer and a package identity', () => {
  const source = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
  const tree = ts.createSourceFile('index.ts', source, ts.ScriptTarget.Latest, true)
  const call = tree.statements.find(node => ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
    && node.expression.expression.getText(tree) === 'ipcMain.handle' && node.expression.arguments[0]?.text === 'extensions.icon').expression
  const compiled = ts.transpileModule(`const handle = ${call.arguments[1].getText(tree)};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const trusted = {}, calls = []
  const handle = new Function('isTrustedBackendRenderer', 'extensionInstaller', `${compiled}; return handle`)(
    sender => sender === trusted, { icon: id => { calls.push(id); return 'image' } })
  assert.equal(handle({ sender: {} }, 'org.example.icon'), null)
  assert.equal(handle({ sender: trusted }, { path: 'secret.png' }), null)
  assert.deepEqual(calls, [])
  assert.equal(handle({ sender: trusted }, 'org.example.icon'), 'image')
  assert.deepEqual(calls, ['org.example.icon'])
})
