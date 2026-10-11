import assert from 'node:assert/strict'
import { execFile, spawn } from 'node:child_process'
import { once } from 'node:events'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import ts from 'typescript'
import { developmentServer, isDevelopmentRendererUrl } from '../src/main/developmentServer.ts'
import { startDevelopment, startupFailure } from '../scripts/dev.mjs'
import { ApplicationLifecycle } from '../src/main/appLifecycle.ts'

function fixture(t, dotenv) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-dev-port-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.mkdirSync(path.join(root, 'electron'))
  fs.writeFileSync(path.join(root, 'electron', 'package.json'), '{"type":"module"}')
  fs.writeFileSync(path.join(root, 'electron', 'vite.config.ts'), 'export default { optimizeDeps: { noDiscovery: true, include: [] } }')
  fs.writeFileSync(path.join(root, 'electron', 'index.html'), '<title>Development port fixture</title>')
  if (dotenv !== undefined) fs.writeFileSync(path.join(root, '.env'), dotenv)
  return root
}

test('missing port defaults to 5173; only the root .env supplies file configuration', t => {
  const root = fixture(t)
  fs.writeFileSync(path.join(root, 'electron', '.env'), 'AMADEUS_DEV_PORT=15173')
  assert.deepEqual(developmentServer(root, {}), { host: 'localhost', port: 5173, origin: 'http://localhost:5173' })
  fs.writeFileSync(path.join(root, '.env'), 'UNRELATED_SECRET=synthetic-only\n')
  assert.equal(developmentServer(root, {}).port, 5173)
})

test('quoted dotenv port is read without importing unrelated environment; process value wins', t => {
  const root = fixture(t, 'AMADEUS_DEV_PORT="15173" # local override\nUNRELATED_SECRET=synthetic-only\n')
  const environment = {}
  assert.equal(developmentServer(root, environment).port, 15173)
  assert.deepEqual(environment, {})
  assert.equal(developmentServer(root, { AMADEUS_DEV_PORT: '25173' }).port, 25173)
  assert.equal(developmentServer(root, { AMADEUS_DEV_PORT: '80' }).origin, 'http://localhost')
  assert.equal(developmentServer(root, { AMADEUS_DEV_PORT: '65535' }).port, 65535)
})

test('invalid explicit ports fail rather than silently falling back or exposing input', t => {
  const root = fixture(t, 'AMADEUS_DEV_PORT=15173')
  for (const value of ['', ' ', '0', '-1', '65536', '1.5', '1e4', '0x1000', '${PORT}', 'secret-not-a-port']) {
    assert.throws(() => developmentServer(root, { AMADEUS_DEV_PORT: value }), error => {
      assert.match(error.message, /AMADEUS_DEV_PORT.*1.*65535/)
      assert.ok(!error.message.includes('secret-not-a-port'))
      return true
    })
  }
  fs.writeFileSync(path.join(root, '.env'), 'AMADEUS_DEV_PORT=invalid')
  assert.throws(() => developmentServer(root, {}), /AMADEUS_DEV_PORT/)
  assert.equal(developmentServer(root, { AMADEUS_DEV_PORT: '15173' }).port, 15173)
})

test('development renderer trust remains limited to the selected origin and shell path', () => {
  for (const origin of ['http://localhost:5173', 'http://localhost:15173', 'http://localhost']) {
    assert.equal(isDevelopmentRendererUrl(`${origin}/?page=work`, origin), true)
    for (const target of ['http://localhost:25173/', 'http://127.0.0.1:15173/',
      'https://localhost:15173/', 'http://localhost.evil.example:15173/',
      `${origin}/other.html`, origin.replace('http://', 'http://user@'), 'file:///index.html']) {
      assert.equal(isDevelopmentRendererUrl(target, origin), false, target)
    }
  }
})

const mainSource = fs.readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8')
const mainAst = ts.createSourceFile('index.ts', mainSource, ts.ScriptTarget.ES2022, true)
const launcherSetup = mainAst.statements.find(node => ts.isIfStatement(node)
  && node.expression.getText(mainAst).includes("process.argv.includes('--amadeus-dev-launcher')"))
const setupLauncher = new Function('process', 'app', 'applicationLifecycle',
  ts.transpileModule(launcherSetup.getText(mainAst), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText)

test('launcher quit, disconnect and console signals share one Electron cleanup request', () => {
  for (const trigger of ['quit-message', 'disconnect', 'SIGINT', 'SIGTERM', 'already-disconnected']) {
    const channel = Object.assign(new EventEmitter(), { argv: ['--amadeus-dev-launcher'], connected: trigger !== 'already-disconnected' })
    const lifecycle = new ApplicationLifecycle()
    let quits = 0
    setupLauncher(channel, { isPackaged: false, quit() { quits++; lifecycle.beginQuit(true) } }, lifecycle)
    channel.emit('message', 'unrelated')
    if (trigger === 'quit-message') channel.emit('message', 'amadeus-dev-quit')
    else if (trigger !== 'already-disconnected') channel.emit(trigger)
    assert.equal(quits, 1, trigger)
    channel.emit('message', 'amadeus-dev-quit')
    channel.emit('SIGINT')
    assert.equal(quits, 1, 'repeated signals must not skip pending cleanup')
  }
})

test('ordinary and packaged Electron launches do not accept development launcher commands', () => {
  for (const [isPackaged, argv] of [[false, []], [true, ['--amadeus-dev-launcher']]]) {
    const channel = Object.assign(new EventEmitter(), { argv, connected: false })
    let quits = 0
    setupLauncher(channel, { isPackaged, quit() { quits++ } }, new ApplicationLifecycle())
    channel.emit('message', 'amadeus-dev-quit')
    channel.emit('disconnect')
    assert.equal(quits, 0)
  }
})

test('quit during the backend health probe cannot spawn a new backend after cleanup', async () => {
  const source = mainAst.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'launchBackend').getText(mainAst)
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const lifecycle = new ApplicationLifecycle()
  const launchBackend = new Function('backendHealthStatus', 'applicationLifecycle', 'getPythonCommand',
    compiled + '; return launchBackend;')(
    async () => { lifecycle.beginQuit(false); return 'absent' }, lifecycle,
    () => { throw new Error('Backend launch must not proceed after quit intent') },
  )
  await launchBackend()
})

function mainFunction(name, devServer) {
  const source = mainAst.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name).getText(mainAst)
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return new Function('devServer', 'isDevelopmentRendererUrl', 'path', '__dirname', 'fileURLToPath', 'RENDERER_ENTRY_PATH',
    compiled + `; return ${name}`)(devServer, isDevelopmentRendererUrl, path, '/app/dist/main',
    url => decodeURIComponent(url.pathname), path.resolve('/app/dist/renderer/index.html'))
}

test('actual Electron auxiliary windows and trust checks share the port; production rejects dev URLs', () => {
  for (const port of [5173, 15173]) {
    const devServer = { port, origin: `http://localhost:${port}` }
    const trusted = mainFunction('isTrustedRendererShellUrl', devServer)
    for (const name of ['workOverlayUrl', 'workGlowUrl', 'workPreviewShellUrl']) {
      const url = mainFunction(name, devServer)('preview-fixture')
      assert.equal(new URL(url).origin, devServer.origin)
      assert.equal(trusted(url), true)
    }
    assert.equal(trusted(`http://localhost:${port === 5173 ? 15173 : 5173}/`), false)
    assert.equal(mainFunction('isTrustedRendererShellUrl', null)(devServer.origin), false)
  }
})

for (const [label, dotenv, port] of [['default', undefined, 5173], ['custom root .env', 'AMADEUS_DEV_PORT=15173', 15173]]) {
  test(`real Vite serves the ${label} port before the child starts, then closes on child exit`, { timeout: 20000 }, async t => {
    const root = fixture(t, dotenv)
    let childPort
    const session = await startDevelopment({
      projectRoot: root, environment: { ...process.env, AMADEUS_DEV_PORT: undefined }, withElectron: true,
      startElectron(_root, environment) {
        childPort = environment.AMADEUS_DEV_PORT
        return spawn(process.execPath, ['-e', `fetch('http://localhost:' + process.env.AMADEUS_DEV_PORT).then(async r => {
          if (!(await r.text()).includes('Development port fixture')) process.exit(2);
        }).catch(error => { console.error(error.cause ?? error); process.exit(3) })`], { env: environment, stdio: ['ignore', 'ignore', 'inherit'], windowsHide: true })
      },
    })
    t.after(() => session.close())
    assert.equal(childPort, String(port))
    assert.equal(session.server.httpServer.address().port, port)
    assert.equal(await session.exited, 0)
    assert.equal(session.server.httpServer.listening, false)
  })
}

test('occupied port fails with a useful diagnostic and never launches a child or switches ports', { timeout: 20000 }, async t => {
  const blocker = net.createServer()
  blocker.listen(0, 'localhost')
  await once(blocker, 'listening')
  t.after(() => new Promise(resolve => blocker.close(resolve)))
  const port = blocker.address().port
  const root = fixture(t, `AMADEUS_DEV_PORT=${port}`)
  let launched = false
  await assert.rejects(startDevelopment({ projectRoot: root, environment: {}, withElectron: true,
    startElectron() { launched = true },
  }), error => {
    assert.equal(error.cause.code, 'EADDRINUSE')
    assert.match(error.message, /AMADEUS_DEV_PORT=15173/)
    return true
  })
  assert.equal(launched, false)
})

test('EACCES from the listening socket closes startup and retains Windows diagnostic guidance', { timeout: 20000 }, async t => {
  const root = fixture(t)
  t.mock.method(net.Server.prototype, 'listen', function () {
    queueMicrotask(() => this.emit('error', Object.assign(new Error('Synthetic bind denial'), { code: 'EACCES' })))
    return this
  })
  let launched = false
  await assert.rejects(startDevelopment({ projectRoot: root, environment: {}, withElectron: true,
    startElectron() { launched = true },
  }), error => {
    assert.equal(error.cause.code, 'EACCES')
    const message = startupFailure(error.cause, 'http://localhost:5173', 'win32')
    assert.match(message, /may be reserved/)
    assert.match(message, /excludedportrange/)
    assert.match(message, /AMADEUS_DEV_PORT=15173/)
    return true
  })
  assert.equal(launched, false)
})

test('Electron spawn failure releases the Vite listener', { timeout: 20000 }, async t => {
  const root = fixture(t, 'AMADEUS_DEV_PORT=15173')
  await assert.rejects(startDevelopment({ projectRoot: root, environment: {}, withElectron: true,
    startElectron() { return spawn(path.join(root, 'missing-electron'), [], { windowsHide: true }) },
  }), /ENOENT/)
  const session = await startDevelopment({ projectRoot: root, environment: {} })
  await session.close()
  assert.equal(session.server.httpServer.listening, false)
})

test('explicit shutdown requests child-owned cleanup before releasing the listening port', { timeout: 20000 }, async t => {
  const root = fixture(t, 'AMADEUS_DEV_PORT=15173')
  const cleaned = path.join(root, 'child-cleaned')
  const session = await startDevelopment({ projectRoot: root, environment: {}, withElectron: true,
    startElectron() { return spawn(process.execPath, ['-e', `process.on('message', message => {
      if (message !== 'amadeus-dev-quit') process.exit(2);
      require('node:fs').writeFileSync(process.argv[1], 'cleaned'); process.disconnect();
    })`, cleaned], { stdio: ['ignore', 'ignore', 'ignore', 'ipc'], windowsHide: true }) },
  })
  await session.close(130)
  assert.equal(await session.exited, 130)
  assert.ok(session.child.exitCode !== null || session.child.signalCode !== null)
  assert.equal(fs.readFileSync(cleaned, 'utf8'), 'cleaned')
  assert.equal(session.server.httpServer.listening, false)
})

test('the real CLI exits nonzero for invalid configuration and occupied ports without hanging', { timeout: 30000 }, async t => {
  const blocker = net.createServer()
  blocker.listen(0, 'localhost')
  await once(blocker, 'listening')
  t.after(() => new Promise(resolve => blocker.close(resolve)))
  const cli = fileURLToPath(new URL('../scripts/dev.mjs', import.meta.url))
  for (const port of ['invalid', String(blocker.address().port)]) {
    await assert.rejects(promisify(execFile)(process.execPath, ['--experimental-strip-types', cli, '--electron'], {
      env: { ...process.env, AMADEUS_DEV_PORT: port }, timeout: 10000, windowsHide: true,
    }), error => {
      assert.equal(error.code, 1)
      assert.match(error.stderr, /AMADEUS_DEV_PORT/)
      if (port !== 'invalid') assert.match(error.stderr, /EADDRINUSE/)
      return true
    })
  }
})
