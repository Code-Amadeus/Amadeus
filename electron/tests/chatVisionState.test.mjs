import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

// Run ChatPage's actual config effect so initial loading and cross-page sync
// both project the backend's enabled flag and remembered mode into the button.
const source = fs.readFileSync(new URL('../src/renderer/components/ChatPage.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('ChatPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let configEffect
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
    && node.arguments[0]?.getText(tree).includes("subscribe('system.config'")
    && node.arguments[0]?.getText(tree).includes('setVisionVideoMode')) {
    assert.equal(configEffect, undefined)
    configEffect = node.arguments[0].getText(tree)
  }
  ts.forEachChild(node, visit)
}
visit(tree)
assert.ok(configEffect)
const compiled = ts.transpileModule(`const configEffect = ${configEffect}; configEffect()`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

async function harness(config) {
  const state = { watching: false, requests: [] }
  let notify
  const context = vm.createContext({
    connected: true,
    send: async (method, params) => {
      state.requests.push({ method, params })
      assert.equal(method, 'system.get_config')
      return config
    },
    subscribe: (method, callback) => {
      assert.equal(method, 'system.config')
      notify = callback
      return () => {}
    },
    setProvider: () => {},
    setCanUseMultimodal: () => {},
    setVisionVideoMode: value => { state.watching = value },
  })
  vm.runInContext(compiled, context)
  await Promise.resolve()
  return { state, notify }
}

test('chat startup shows continuous vision only when vision is enabled and watching', async () => {
  for (const enabled of [false, true]) {
    for (const mode of ['off', 'on_demand', 'watching', 'self_aware']) {
      const page = await harness({ vision_enabled: enabled, vision_mode: mode })
      assert.equal(page.state.watching, enabled && mode === 'watching', `${enabled}/${mode}`)
      assert.equal(page.state.requests.length, 1)
    }
  }
  const page = await harness({ vision_mode: 'watching' })
  assert.equal(page.state.watching, false, 'a remembered mode alone does not enable capture')
})

test('config notifications keep disabled vision off while retaining the mode for reenable', async () => {
  const page = await harness({ vision_enabled: true, vision_mode: 'watching' })
  assert.equal(page.state.watching, true)
  for (const envelope of [values => ({ values }), values => values]) {
    for (const [enabled, mode, watching] of [
      [false, 'watching', false],
      [true, 'watching', true],
      [true, 'on_demand', false],
      [false, 'on_demand', false],
    ]) {
      page.notify(envelope({ vision_enabled: enabled, vision_mode: mode }))
      assert.equal(page.state.watching, watching, `${enabled}/${mode}`)
    }
  }
  assert.equal(page.state.requests.length, 1, 'display synchronization never changes backend settings')
})
