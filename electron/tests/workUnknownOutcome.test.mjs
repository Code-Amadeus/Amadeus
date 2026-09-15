import assert from 'node:assert/strict'
import test from 'node:test'

import { createServer } from 'vite'

import {
  workItemAttentionActionLabel,
} from '../src/renderer/components/work/workProjection.ts'
import { readFileSync } from 'node:fs'

const orphanedRun = {
  run_id: 'run-unknown',
  provider: 'codex_app_server',
  task: 'Apply the requested change',
  cwd: null,
  status: 'orphaned',
  result: '',
  error: 'turn/start acknowledgement was lost',
  metadata: {},
  events: [],
}

test('Work projection calls orphaned an unknown outcome action', () => {
  assert.equal(workItemAttentionActionLabel({
    execution: 'orphaned',
    attention: 'error',
  }), 'Reconcile outcome')
  const card = readFileSync(
    new URL('../src/renderer/components/ChatWorkActivityCard.tsx', import.meta.url),
    'utf8',
  )
  assert.match(card, /orphaned: 'Outcome unknown'/)
  assert.doesNotMatch(card, /activity\.status === 'orphaned'/)
})

test('orphaned status and risk guidance require reconciliation before retry', async () => {
  process.env.VITE_CONFIG_NATIVE_IGNORE_WARNING = 'true'
  const vite = await createServer({
    appType: 'custom',
    server: { middlewareMode: true },
  })
  try {
    const { providerRunToWorkTurn } = await vite.ssrLoadModule(
      '/src/renderer/components/work/workModels.ts',
    )
    const { STATUS_META } = await vite.ssrLoadModule(
      '/src/renderer/components/work/workState.ts',
    )
    const turn = providerRunToWorkTurn(orphanedRun, undefined, {
      cwd: '',
      provider: 'codex_app_server',
    })
    const risk = turn.risks[0]

    assert.equal(STATUS_META.orphaned.label, 'Outcome unknown')
    assert.equal(turn.status, 'blocked')
    assert.match(risk.summary, /acknowledgement was lost/)
    assert.match(risk.mitigation, /Reconcile/)
    assert.match(risk.mitigation, /before any retry/)
    assert.doesNotMatch(risk.mitigation, /retry the failed instruction/)
  } finally {
    await vite.close()
  }
})
