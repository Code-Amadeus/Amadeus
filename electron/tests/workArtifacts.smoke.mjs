// Run with Electron. All Host responses are synthetic; production React/CSS render.
import { app, BrowserWindow } from 'electron'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const electronRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.join(electronRoot, '../output/diagnostics/work-artifacts-ui')
app.setPath('userData', path.join(output, 'electron-profile'))
async function run() {
await fs.mkdir(output, { recursive: true })
await app.whenReady()
const { createServer } = await import('vite')
const server = await createServer({ configFile: path.join(electronRoot, 'vite.config.ts'), configLoader: 'runner', root: electronRoot,
  cacheDir: path.join(output, 'vite-cache'), server: { host: '127.0.0.1', port: 0, strictPort: false },
  plugins: [{ name: 'work-artifacts-smoke', configureServer(server) {
    server.middlewares.use(async (request, response, next) => {
      if (request.url?.split('?')[0] !== '/work-artifacts-smoke.html') return next()
      response.setHeader('Content-Type', 'text/html; charset=utf-8')
      response.end(await server.transformIndexHtml('/work-artifacts-smoke.html', '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/tests/fixtures/workArtifacts.tsx"></script></body></html>'))
    })
  } }],
})
await server.listen()
const url = `http://127.0.0.1:${server.httpServer.address().port}/work-artifacts-smoke.html`
const window = new BrowserWindow({ width: 1180, height: 1050, show: false,
  webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false, offscreen: true } })
const ui = source => window.webContents.executeJavaScript(source)
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
async function until(source, message) { const deadline = Date.now() + 20000
  while (Date.now() < deadline) { if (await ui(source)) return; await pause(50) }
  throw new Error(message)
}
async function click(label) { const clicked = await ui(`(() => { const button = [...document.querySelectorAll('button')].find(node => node.textContent.trim() === ${JSON.stringify(label)}); if (!button || button.disabled) return false; button.click(); return true })()`)
  assert.equal(clicked, true, `Enabled button missing: ${label}`); await pause(80)
}
const report = { passed: false, checks: [], screenshots: [] }
try {
  await window.loadURL(url)
  await until("document.body.textContent.includes('audit.json')", 'Files did not render')
  for (const [title, content] of [['audit.json','{"rows":5}'],['orders.csv','id,amount'],['issues.txt','negative amount'],['notes.md','<script>']]) {
    await ui(`document.querySelector('button[aria-label="View ${title}"]').click()`)
    await until(`document.querySelector('pre')?.textContent.includes(${JSON.stringify(content)})`, 'Preview did not load ' + title)
    await click('Close preview')
  }
  assert.equal(await ui('window.attacked'), undefined)
  assert.equal(await ui("document.body.textContent.includes('Execution: succeeded') && document.body.textContent.includes('Awaiting review')"), true)
  assert.deepEqual(await ui('window.artifactFixture.reads'), [0,1,2,3].map(index => ({ work_item_id:'audit-work', artifact_id:`file-${index}` })))
  await ui('window.artifactFixture.pause = true')
  await ui(`document.querySelector('button[aria-label="View audit.json"]').click()`)
  await until("document.body.textContent.includes('Loading file') && !document.querySelector('pre')", 'Loading reused another file content')
  await ui('window.artifactFixture.pause = false')
  await ui(`document.querySelector('button[aria-label="View issues.txt"]').click()`)
  await until("document.querySelector('pre')?.textContent.includes('negative amount')", 'Second file did not load')
  await ui('window.artifactFixture.release()')
  await pause(100)
  assert.equal(await ui("document.querySelector('pre').textContent.includes('Late response')"), false)
  await ui('window.artifactFixture.missing = true')
  await ui(`document.querySelector('button[aria-label="View audit.json"]').click()`)
  await until("document.querySelector('[role=alert]')?.textContent.includes('missing')", 'Missing file feedback absent')
  assert.equal(await ui("document.querySelector('pre') === null"), true)
  assert.equal(await ui("[...document.querySelectorAll('button')].some(node=>node.textContent.trim()==='Open')"), false)
  await click('Interactive apps · 1')
  await until("document.body.textContent.includes('Verified application')", 'Application view missing')
  assert.equal(await ui("document.querySelector('button[aria-label=\"View audit.json\"]') === null"), true)
  assert.equal(await ui("document.body.textContent.includes('Application files · 1')"), true)
  await ui("[...document.querySelectorAll('summary')].find(node=>node.textContent.includes('Application files')).click()")
  await ui(`document.querySelector('button[aria-label="View auip.manifest.json"]').click()`)
  await until("document.querySelector('pre')?.textContent.includes('auip/v0')", 'Application member preview missing')
  assert.deepEqual(await ui('window.artifactFixture.reads.at(-1)'), { work_item_id: 'app-work', artifact_id: 'manifest' })
  await click('Open')
  assert.deepEqual(await ui('window.artifactFixture.opened'), ['app'])
  await click('Interact with Amadeus')
  assert.equal(await ui('window.artifactFixture.apps'), 1)
  await click('Refresh')
  assert.equal(await ui("document.querySelector('[aria-label^=\"File preview:\"]') === null"), true)
  await window.setSize(460, 760)
  await pause(100)
  assert.equal(await ui('document.documentElement.scrollWidth <= innerWidth'), true)
  report.passed = true
  report.checks = ['JSON CSV TXT Markdown previews', 'registered IDs only', 'text escaped', 'execution vs review state', 'missing file has no stale content', 'AUIP open and interact unchanged', 'refresh and narrow layout', 'late response cannot replace another file', 'separate file and AUIP views with application members']
} finally {
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
  window.destroy()
  await server.close()
}
}
run().then(() => app.exit(0), error => { console.error(error); app.exit(1) })
