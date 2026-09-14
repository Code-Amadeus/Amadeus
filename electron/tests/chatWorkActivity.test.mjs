import assert from 'node:assert/strict'
import test from 'node:test'

import {
  activitiesFromProviderRuns,
  applyProviderEvent,
  applyProviderResult,
} from '../src/renderer/components/chatWorkActivity.ts'

const orphanedResult = {
  run_id: 'run-orphaned',
  provider: 'codex_app_server',
  status: 'orphaned',
  task: 'Continue the existing work',
  created_at: 100,
  updated_at: 120,
  error: 'turn/start acknowledgement was lost',
  metadata: {
    session_id: 'session-a',
    turn_id: 'turn-a',
    work: {
      work_item_id: 'work-a',
      attempt_id: 'attempt-a',
    },
  },
}

const orphanedEvent = {
  provider: orphanedResult.provider,
  run_id: orphanedResult.run_id,
  type: 'run.status',
  sequence: 3,
  observed_at: 119,
  payload: {
    status: 'orphaned',
    error: orphanedResult.error,
  },
  metadata: orphanedResult.metadata,
}

test('live orphaned result becomes a non-terminal recovery activity', () => {
  const [activity] = applyProviderResult([], orphanedResult)

  assert.equal(activity.status, 'orphaned')
  assert.deepEqual(activity.entries.map(entry => ({
    id: entry.id,
    state: entry.state,
    title: entry.title,
  })), [{
    id: 'recovery',
    state: 'attention',
    title: 'Outcome unknown — recovery required',
  }])
  assert.equal(activity.entries.some(entry => entry.id === 'terminal'), false)
  assert.equal(activity.entries.some(entry => /Work (?:failed|completed)/.test(entry.title)), false)
})

test('hydrated orphaned run matches the live recovery projection', () => {
  const live = applyProviderResult(
    applyProviderEvent([], orphanedEvent),
    orphanedResult,
  )
  const hydrated = activitiesFromProviderRuns([{
    ...orphanedResult,
    events: [orphanedEvent],
  }], 'session-a')

  assert.deepEqual(hydrated, live)
  assert.equal(hydrated[0].entries.filter(entry => entry.id === 'recovery').length, 1)
})

test('a later definite result replaces recovery with existing terminal semantics', () => {
  const cases = [
    { status: 'done', projected: 'succeeded', state: 'succeeded', title: 'Work completed' },
    { status: 'error', projected: 'failed', state: 'failed', title: 'Work failed' },
    { status: 'cancelled', projected: 'cancelled', state: 'attention', title: 'Work cancelled' },
  ]

  for (const expected of cases) {
    const orphaned = applyProviderResult([], orphanedResult)
    const [activity] = applyProviderResult(orphaned, {
      ...orphanedResult,
      status: expected.status,
      updated_at: 130,
      error: expected.status === 'error' ? 'definite provider failure' : '',
      result: expected.status === 'done' ? 'finished' : '',
    })

    assert.equal(activity.status, expected.projected)
    assert.equal(activity.entries.some(entry => entry.id === 'recovery'), false)
    assert.deepEqual(activity.entries.map(entry => ({
      id: entry.id,
      state: entry.state,
      title: entry.title,
    })), [{
      id: 'terminal',
      state: expected.state,
      title: expected.title,
    }])
  }
})

test('cooperative permission denial replaces the exact nested request', () => {
  const metadata = {
    session_id: 'session-cooperative',
    turn_id: 'turn-cooperative',
    cooperative_context_id: 'context-cooperative',
  }
  const requested = applyProviderEvent([], {
    provider: 'codex',
    run_id: 'run-cooperative',
    type: 'permission.requested',
    sequence: 2,
    observed_at: 200,
    metadata,
    payload: {
      permissionRequest: {
        request_id: 'native-permission-1',
        capability: 'shell.execute',
        action: 'execute_command',
        reason: 'The command needs elevated access.',
        scope: ['C:/outside'],
      },
    },
  })
  const [activity] = applyProviderEvent(requested, {
    provider: 'codex',
    run_id: 'run-cooperative',
    type: 'permission.denied',
    sequence: 3,
    observed_at: 201,
    metadata,
    payload: {
      request_id: 'native-permission-1',
      decision: 'deny',
      automatic: true,
      reason: 'cooperative_permission_policy_deny',
    },
  })

  assert.deepEqual(requested[0].entries[0].permission, {
    requestId: 'native-permission-1',
    options: [],
  })

  assert.equal(activity.turnId, 'turn-cooperative')
  assert.deepEqual(activity.entries.map(entry => ({
    id: entry.id,
    state: entry.state,
    title: entry.title,
  })), [{
    id: 'permission:native-permission-1',
    state: 'failed',
    title: 'Permission denied',
  }])

  const [reversed] = applyProviderEvent(
    applyProviderEvent([], {
      provider: 'codex',
      run_id: 'run-cooperative',
      type: 'permission.denied',
      sequence: 3,
      observed_at: 201,
      metadata,
      payload: { request_id: 'native-permission-1', decision: 'deny', automatic: true },
    }),
    {
      provider: 'codex',
      run_id: 'run-cooperative',
      type: 'permission.requested',
      sequence: 2,
      observed_at: 200,
      metadata,
      payload: {
        permissionRequest: {
          request_id: 'native-permission-1',
          reason: 'The command needs elevated access.',
        },
      },
    },
  )
  assert.equal(reversed.entries[0].title, 'Permission denied')
  assert.equal(reversed.entries[0].state, 'failed')
})

test('cooperative permission request keeps only exact bounded card actions', () => {
  const [activity] = applyProviderEvent([], {
    provider: 'codex',
    run_id: 'run-ask',
    type: 'permission.requested',
    observed_at: 300,
    metadata: { session_id: 'session-ask', turn_id: 'turn-ask' },
    payload: { permissionRequest: {
      request_id: 'native-ask',
      options: ['approve_once', 'deny', 'allow_once', 'always_allow'],
    } },
  })

  assert.deepEqual(activity.entries[0].permission, {
    requestId: 'native-ask',
    options: ['allow_once', 'deny'],
  })
})
