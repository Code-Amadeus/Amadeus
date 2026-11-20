import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { normalizeWorkProjection } from '../src/renderer/components/work/workProjection.ts'

const permission = {
  id: 'permission-a', ownerKind: 'work_attempt', workItemId: 'work-a', attemptId: 'attempt-a',
  runId: 'run-a', providerRequestId: 'native-a', status: 'pending',
  capability: 'mcp.tool.call', action: 'call_tool', scope: ['gui-runtime/instant_status'],
  reason: 'Read the current status', options: ['allow_once', 'allow_work', 'deny'],
  mcpCall: { connectionId: 'gui-runtime', toolName: 'instant_status', arguments: { detail: true } },
}

test('automatic Work approval defaults off and belongs to persistent live desktop settings', () => {
  const { catalogApplication, desktopCatalogFields, runtimeCatalogFields } = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))
  assert.equal(desktopCatalogFields.WORK_PERMISSION_AUTO_APPROVE?.default, false)
  assert.equal(runtimeCatalogFields.work_permission_auto_approve?.key, 'WORK_PERMISSION_AUTO_APPROVE')
  assert.equal(catalogApplication('WORK_PERMISSION_AUTO_APPROVE'), 'host')
})

test('automatic approval can be saved on and off across desktop settings reloads', t => {
  const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), {
    electron: { safeStorage: { isEncryptionAvailable: () => false } },
  })
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-work-approval-'))
  const settingsPath = path.join(directory, 'settings.json')
  t.after(() => {
    for (const name of fs.readdirSync(directory)) fs.unlinkSync(path.join(directory, name))
    fs.rmdirSync(directory)
  })
  const open = () => new DesktopSettingsStore(settingsPath, path.join(directory, '.env'))
  let settings = open()
  settings.update({}, { values: { WORK_PERMISSION_AUTO_APPROVE: true } })
  settings = open()
  assert.equal(settings.backendEnvironment({}).WORK_PERMISSION_AUTO_APPROVE, 'true')
  settings.update({}, { values: { WORK_PERMISSION_AUTO_APPROVE: false } })
  assert.equal(open().backendEnvironment({}).WORK_PERMISSION_AUTO_APPROVE, 'false')
})

test('Work projection retains exact pending approval and all active grants independently', () => {
  const grants = [permission, { ...permission, id: 'permission-b' }].map(value => ({
    ...value, status: 'allowed', grantStatus: 'active', useCount: 2,
  }))
  const projection = normalizeWorkProjection({ revision: 'r1', selectedWorkItemId: 'work-a',
    permissionRequest: permission, activePermissionGrants: grants })
  assert.equal(projection.permissionRequest.id, 'permission-a')
  assert.deepEqual(projection.permissionRequest.mcpCall, permission.mcpCall)
  assert.deepEqual(projection.activePermissionGrants.map(value => value.id), ['permission-a', 'permission-b'])
  assert.equal(normalizeWorkProjection({ permissionRequest: null, activePermissionGrants: [] }).permissionRequest, null)
})

test('normal Work permission panel exposes offered decisions, revocation and explicit global confirmation', async t => {
  const vite = await createServer({ appType: 'custom', server: { middlewareMode: true } })
  try {
    const { default: WorkPermissionsPanel } = await vite.ssrLoadModule('/src/renderer/components/work/WorkPermissionsPanel.tsx')
    const { permissionActionPayload } = await vite.ssrLoadModule('/src/renderer/components/work/permissionProjection.ts')
    const actions = []
    const grants = [permission, { ...permission, id: 'permission-b' }].map(value => ({ ...value, grantStatus: 'active' }))
    const tree = WorkPermissionsPanel({ request: permission, grants, busy: false, error: '',
      onDecision: (request, action) => actions.push(permissionActionPayload(request, action, 'r1')), t: value => value })
    const nodes = []
    const visit = value => {
      if (Array.isArray(value)) return value.forEach(visit)
      if (!value || typeof value !== 'object') return
      nodes.push(value)
      visit(value.props?.children)
    }
    visit(tree)
    const buttons = nodes.filter(value => value.type === 'button')
    assert.equal(buttons.length, 5)
    buttons.forEach(value => value.props.onClick())
    assert.deepEqual(actions.map(value => value.action), ['allow_once', 'allow_work', 'deny', 'revoke_work', 'revoke_work'])
    assert.deepEqual(actions.map(value => value.permission_request_id), ['permission-a', 'permission-a', 'permission-a', 'permission-a', 'permission-b'])
    for (const action of actions) {
      assert.equal(action.work_item_id, 'work-a')
      assert.equal(action.attempt_id, 'attempt-a')
      assert.equal(action.revision, 'r1')
      assert.equal('mcpCall' in action, false)
    }
    const { renderToStaticMarkup } = await import('react-dom/server')
    const html = renderToStaticMarkup(tree)
    assert.match(html, /gui-runtime/)
    assert.match(html, /instant_status/)
    assert.match(html, /detail/)
    assert.match(html, /exactly the same arguments/)
    let disabled = false
    const automaticTree = WorkPermissionsPanel({ request: null, grants: [], automatic: true, busy: false, error: '',
      onDecision() {}, onSetAutomatic: value => { disabled = value === false }, t: value => value })
    nodes.length = 0
    visit(automaticTree)
    const offButton = nodes.find(value => value.type === 'button')
    assert.ok(offButton, 'Enabled automatic approval has no off control')
    offButton.props.onClick()
    assert.equal(disabled, true)
    assert.match(renderToStaticMarkup(automaticTree), /Work and Cooperative chats, including MCP and shell/)
    const deniedTree = WorkPermissionsPanel({ request: { ...permission, options: ['deny'] }, grants: [],
      busy: false, error: '', onDecision() {}, t: value => value })
    assert.doesNotMatch(renderToStaticMarkup(deniedTree), /Allow once|Allow identical calls/)
    let enabled
    const shellTree = WorkPermissionsPanel({ request: { ...permission, capability: 'shell.execute', mcpCall: undefined,
      options: ['allow_once', 'deny'] }, grants: [], automatic: false, busy: false, error: '',
      onDecision() {}, onSetAutomatic: value => { enabled = value }, t: value => value })
    nodes.length = 0
    visit(shellTree)
    const automaticButton = nodes.find(value => value.type === 'button'
      && value.props.children === 'Always automatically approve Work and Chat')
    assert.ok(automaticButton, 'Generic shell approval has no persistent automatic approval option')
    let confirmed = false
    const confirmations = []
    const originalWindow = globalThis.window
    t.after(() => {
      if (originalWindow === undefined) delete globalThis.window
      else globalThis.window = originalWindow
    })
    globalThis.window = { confirm: message => {
      confirmations.push(message)
      return confirmed
    } }
    automaticButton.props.onClick()
    assert.equal(enabled, undefined, 'Cancelling global confirmation must not persist the setting')
    confirmed = true
    automaticButton.props.onClick()
    assert.equal(enabled, true)
    assert.equal(confirmations.length, 2)
    assert.match(confirmations[0], /Work and Cooperative chats/)
    assert.match(confirmations[0], /MCP and shell/)
    assert.match(confirmations[0], /pending requests/)
    assert.match(confirmations[0], /across restarts/)
    const shellHtml = renderToStaticMarkup(shellTree)
    assert.match(shellHtml, /all Work and Cooperative chats, including MCP and shell/)
    assert.match(shellHtml, /Saved across restarts until you turn it off/)
    assert.doesNotMatch(shellHtml, /Allow identical calls/)
  } finally {
    await vite.close()
  }
})
