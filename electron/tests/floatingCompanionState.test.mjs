import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

import {
  applyCompanionProviderEvent,
  deriveFloatingCompanionPresence,
  companionTaskHeading,
  companionCardOffset,
  groupCompanionTasks,
} from '../src/renderer/components/floatingCompanionState.ts'

const running = {
  run_id: 'run-1',
  provider: 'codex',
  task: 'Build the floating companion',
  status: 'running',
  events: [],
}

test('task offsets are stable, varied, and bounded within layout gutters', () => {
  const offsets = Array.from({ length: 100 }, (_, index) => companionCardOffset(`task-${index}`))
  assert.deepEqual(companionCardOffset('task-1'), companionCardOffset('task-1'))
  assert.ok(new Set(offsets.map(offset => `${offset.x}:${offset.y}`)).size > 90)
  assert.ok(offsets.every(({ x, y }) => x >= 0 && x <= 56 && y >= 0 && y <= 84))
})

test('provider source timestamps survive conversion and polling', () => {
  const run = { ...running, events: [{ type: 'tool.call', time_ms: 1234 }, { type: 'tool.result', time_ms: 1500 }] }
  assert.equal(deriveFloatingCompanionPresence([run]).tasks[0].lastActivityAt, 1500)
})

test('project identity groups tasks and their agents; identical names do not merge different projects', () => {
  const a = { id: 'a', title: 'task a', projectId: 'one', projectName: '同名项目' }
  const b = { id: 'b', title: 'task b', projectId: 'one', projectName: '同名项目' }
  const c = { id: 'c', title: 'task c', projectId: 'two', projectName: '同名项目' }
  const child = { id: 'child', title: 'agent', parentTaskId: 'a' }
  const groups = groupCompanionTasks([a,c,b,child])
  assert.equal(groups.length, 2)
  assert.deepEqual(groups[0].tasks.map(t => t.id), ['a','b','child'])
  assert.equal(groups[1].tasks[0].id, 'c')
  assert.equal(groupCompanionTasks([{ id:'x', title:'no project' },{id:'y', title:'no project'}]).length, 2)
})

test('cards prefer actual project names and keep titles for projectless tasks', () => {
  assert.equal(companionTaskHeading({ projectName: 'Amadeus', title: '随便的标题' }), 'Amadeus')
  assert.equal(companionTaskHeading({ projectName: '', title: '独立对话' }), '独立对话')
})

test('projectless side tasks keep their parent conversation group when its card is absent', () => {
  const parent = { id: 'parent', title: 'Parent conversation' }
  const first = { id: 'first', title: 'Side A', parentTaskId: parent.id, sourceKind: 'sidechat' }
  const second = { id: 'second', title: 'Side B', parentTaskId: parent.id, sourceKind: 'sidechat' }
  const absent = groupCompanionTasks([first, second])
  const present = groupCompanionTasks([parent, first, second])
  assert.equal(absent.length, 1)
  assert.equal(absent[0].id, present[0].id)
  assert.deepEqual(absent[0].tasks, [first, second])
  assert.equal(groupCompanionTasks([first, {...second, parentTaskId: 'another-parent'}]).length, 2)
})

test('active provider work becomes a compact running presence', () => {
  const presence = deriveFloatingCompanionPresence([running])
  assert.equal(presence.phase, 'running')
  assert.equal(presence.running, 1)
  assert.equal(presence.headline, running.task)
})

test('permission requests take precedence over ordinary running work', () => {
  const runs = applyCompanionProviderEvent([running], {
    provider: 'codex',
    run_id: 'run-1',
    type: 'permission.requested',
    payload: { reason: 'Approve the command' },
  })
  const presence = deriveFloatingCompanionPresence(runs)
  assert.equal(presence.phase, 'attention')
  assert.equal(presence.needsAttention, 1)
})

test('a later permission resolution clears companion attention', () => {
  let runs = applyCompanionProviderEvent([running], {
    provider: 'codex',
    run_id: 'run-1',
    type: 'permission.requested',
    payload: { request_id: 'permission-1', reason: 'Approve the command' },
  })
  runs = applyCompanionProviderEvent(runs, {
    provider: 'codex',
    run_id: 'run-1',
    type: 'permission.allowed',
    payload: { request_id: 'permission-1' },
  })
  const presence = deriveFloatingCompanionPresence(runs)
  assert.equal(presence.phase, 'running')
  assert.equal(presence.needsAttention, 0)
})

test('terminal events preserve ready and blocked outcomes', () => {
  const done = applyCompanionProviderEvent([running], {
    provider: 'codex', run_id: 'run-1', type: 'run.finished', payload: {},
  })
  assert.equal(deriveFloatingCompanionPresence(done).phase, 'ready')

  const failed = applyCompanionProviderEvent([running], {
    provider: 'codex', run_id: 'run-1', type: 'run.failed', payload: { error: 'tests failed' },
  })
  assert.equal(deriveFloatingCompanionPresence(failed).phase, 'blocked')
})

test('the companion applies the shared provider-event visibility gate', () => {
  const component = fs.readFileSync(
    new URL('../src/renderer/components/FloatingCompanion.tsx', import.meta.url),
    'utf8',
  )
  assert.match(component, /if \(!isVisibleProviderEvent\(event\)\) return/)
})
