import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const page = fs.readFileSync(new URL('../src/renderer/components/ContinuityPage.tsx', import.meta.url), 'utf8')
const app = fs.readFileSync(new URL('../src/renderer/App.tsx', import.meta.url), 'utf8')
const sidebar = fs.readFileSync(new URL('../src/renderer/components/Sidebar.tsx', import.meta.url), 'utf8')

test('C7 Continuity page uses only Host websocket methods for controls', () => {
  for (const method of [
    'continuity.status', 'continuity.memory.list', 'continuity.memory.pin',
    'continuity.memory.forget', 'continuity.index.rebuild',
    'continuity.maintenance.run', 'continuity.life.schedule',
  ]) assert.match(page, new RegExp(method.replaceAll('.', '\\.')))
  assert.doesNotMatch(page, /better-sqlite|node:fs|readFileSync|writeFileSync/i)
})

test('C7 Continuity surface is routed from the trusted desktop navigation', () => {
  assert.match(app, /'continuity'/)
  assert.match(app, /<ContinuityPage send=\{send\} connected=\{connected\}/)
  assert.match(sidebar, /page: 'continuity'/)
  assert.match(page, /SIMULATED_LIFE/)
})
