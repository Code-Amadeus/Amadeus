import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

import {
  isVisibleProviderEvent,
  visibleProviderEvents,
} from '../src/renderer/components/work/providerEventVisibility.ts'

test('delivery receipts never enter Electron Work projections', () => {
  const created = { type: 'run.created', sequence: 1 }
  const receipt = { type: 'context.delivered', sequence: 2 }
  const tool = { type: 'tool.call', sequence: 3 }

  assert.equal(isVisibleProviderEvent(receipt), false)
  assert.equal(isVisibleProviderEvent(created), true)
  assert.deepEqual(visibleProviderEvents([created, receipt, tool]), [created, tool])
})

test('the visibility gate covers list/result normalization, signals, and live events', () => {
  const workRoot = new URL('../src/renderer/components/', import.meta.url)
  const workState = fs.readFileSync(new URL('work/workState.ts', workRoot), 'utf8')
  const signalAdapter = fs.readFileSync(
    new URL('work/providerEventAdapter.ts', workRoot),
    'utf8',
  )
  const workPage = fs.readFileSync(new URL('WorkPage.tsx', workRoot), 'utf8')

  assert.match(workState, /events:\s*visibleProviderEvents\(/)
  assert.match(signalAdapter, /const events = visibleProviderEvents\(run\.events \|\| \[\]\)/)
  assert.match(workPage, /if \(!isVisibleProviderEvent\(event\)\) return/)
})
