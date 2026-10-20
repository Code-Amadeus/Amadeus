import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { EventEmitter } from 'node:events'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
let scenario, options, requests
const transport = { fromPartition: (name, config) => { assert.equal(name, 'amadeus-extension-downloads'); assert.equal(config.cache, false); return { isolated: true } } }
const net = { request: opts => {
  options = opts; requests++
  const request = new EventEmitter()
  request.abort = () => request.emit('abort')
  request.followRedirect = () => { request.followed = true }
  request.end = () => setImmediate(() => scenario(request))
  return request
} }
const { downloadExtension, artifactUrl, httpsUrl } = loadTypeScript(new URL('../src/main/extensionDownloads.ts', import.meta.url), { electron: { net, session: transport } })
function response(request, chunks, length, status = 200) {
  const r = new EventEmitter(); r.statusCode = status; r.headers = length === undefined ? {} : { 'content-length': [String(length)] }
  request.emit('response', r)
  for (const chunk of chunks) r.emit('data', Buffer.from(chunk))
  r.emit('end')
}
test('network boundary rejects invalid URLs and mirrors before requesting bytes', () => {
  for (const url of ['http://a/', 'https://u:p@a/', 'https://a/#x', 'file:///tmp', 'https://a/\n']) assert.throws(() => httpsUrl(url))
  assert.throws(() => artifactUrl('https://a/p.whl', 'a'.repeat(64), 'https://mirror/?token=x'))
})
test('bounded transport, credentials isolation, redirects, cancellation and failure cleanup', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-download-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const cases = [
    ['valid', r => response(r, ['wheel'], 5), true],
    ['decoded response', request => { const r = new EventEmitter(); r.statusCode = 200; r.headers = { 'content-length': '99', 'content-encoding': 'gzip' }; request.emit('response', r); r.emit('data', Buffer.from('wheel')); r.emit('end') }, true],
    ['string header', request => { const r = new EventEmitter(); r.statusCode = 200; r.headers = { 'content-length': '5' }; request.emit('response', r); r.emit('data', Buffer.from('wheel')); r.emit('end') }, true],
    ['stream limit', r => response(r, ['wheel-long']), false],
    ['header limit', r => response(r, ['wheel'], 9), false],
    ['truncated', r => response(r, ['wh'], 5), false],
    ['status', r => response(r, ['wheel'], 5, 404), false],
    ['network', r => r.emit('error', new Error('credential-containing error must not escape')), false],
    ['redirect valid', r => { r.emit('redirect', 302, 'GET', 'https://changed-cdn.example/p.whl'); assert.ok(r.followed); response(r, ['wheel'], 5) }, true],
    ['redirect downgrade', r => r.emit('redirect', 302, 'GET', 'http://a/'), false],
    ['redirect credentials', r => r.emit('redirect', 302, 'GET', 'https://u:p@a/'), false],
    ['redirect loop', r => { for (let i = 0; i < 6; i++) r.emit('redirect', 302, 'GET', 'https://a/') }, false],
  ]
  for (const [name, run, success] of cases) {
    scenario = run; requests = 0
    const target = path.join(root, name + '.whl')
    const operation = downloadExtension('https://origin.example/p.whl', target, 5, new AbortController().signal)
    if (success) { await operation; assert.equal(fs.readFileSync(target, 'utf8'), 'wheel'); fs.unlinkSync(target) }
    else { await assert.rejects(operation); assert.ok(!fs.existsSync(target)) }
    assert.equal(options.credentials, 'omit'); assert.equal(options.useSessionCookies, false); assert.equal(options.redirect, 'manual'); assert.equal(options.bypassCustomProtocolHandlers, true)
    assert.equal(requests, 1)
  }
  const abort = new AbortController(); scenario = () => abort.abort()
  const target = path.join(root, 'cancelled.whl')
  await assert.rejects(downloadExtension('https://a/p.whl', target, 5, abort.signal), /cancelled/)
  assert.ok(!fs.existsSync(target))
  abort.abort()
  await assert.rejects(downloadExtension('https://a/p.whl', target, 5, abort.signal), /cancelled/)
})

test('next process reclaims owned interrupted downloads while retaining unknown directories and live requests', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-download-stages-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const first = loadTypeScript(new URL('../src/main/extensionDownloads.ts', import.meta.url), { electron: { net, session: transport } })
  const old = first.stagingDirectory(root)
  fs.writeFileSync(path.join(old.directory, 'partial.whl'), 'interrupted bytes')
  const live = first.stagingDirectory(root)
  assert.ok(fs.existsSync(old.directory), 'active operation must survive another download')
  const unknown = path.join(root, 'download-ABCDEF'); fs.mkdirSync(unknown); fs.writeFileSync(path.join(unknown, 'private.txt'), 'retained')
  const restarted = loadTypeScript(new URL('../src/main/extensionDownloads.ts', import.meta.url), { electron: { net, session: transport } })
  const current = restarted.stagingDirectory(root)
  assert.ok(!fs.existsSync(old.directory)); assert.ok(!fs.existsSync(live.directory))
  assert.ok(fs.existsSync(path.join(unknown, 'private.txt')))
  current.dispose()
})
