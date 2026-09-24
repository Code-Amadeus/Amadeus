import assert from 'node:assert/strict'
import test from 'node:test'
import { activityFromEvent, mergeActivity, sessionBanner } from '../src/renderer/components/vnPresentation.ts'

const envelope = { event_id: 's:1', event_seq: 1, session_id: 's', emitted_at_ms: 1 }
test('real nested reaction, silence and summary keep the correct speaker and content', () => {
  const payload = { ...envelope, line: { text: 'Game dialogue' }, reaction: { decision: 'speak', speak: { text: '[EMO preset=thinking] Companion answer' } } }
  assert.equal(activityFromEvent('vn.reaction', payload).text, 'Companion answer')
  assert.equal(activityFromEvent('vn.reaction', { ...payload, reaction: { decision: 'silence', speak: null } }), null)
  assert.equal(activityFromEvent('vn.summary', { ...envelope, scene_summary: { summary: 'Story summary' } }).text, 'Story summary')
  assert.equal(activityFromEvent('vn.player.event', { ...envelope, event: { text: 'Question' } }).text, 'Question')
  assert.equal(activityFromEvent('vn.status', { ...envelope, status: 'active' }), null)
})
test('history recovery merges events by identity and order, preserving actual repetitions', () => {
  const line = seq => activityFromEvent('vn.line', { ...envelope, event_id: `s:${seq}`, event_seq: seq, line: { text: 'Repeated line' } })
  const restored = mergeActivity([line(3)], [line(1), line(2), line(3)], 's')
  assert.deepEqual(restored.map(item => item.seq), [1, 2, 3])
  assert.equal(mergeActivity(restored, [], 'new-session').length, 0)
})
test('zero text, lost process, lost source and unavailable model never claim live companionship', () => {
  const launch = { status: 'active', game: { status: 'running' }, hook: { status: 'running' }, bridge: { status: 'running', lineCount: 3 } }
  const runtime = { status: 'active', llm: { configured: true }, capabilities: { interaction: { enabled: true } } }
  assert.equal(sessionBanner(launch, runtime, true, true).tone, 'live')
  for (const changed of [ { bridge: { status: 'running', lineCount: 0 } }, { game: { status: 'exited' } }, { hook: { status: 'exited' } }, { bridge: { status: 'waiting', lineCount: 3 } } ]) {
    assert.notEqual(sessionBanner({ ...launch, ...changed }, runtime, true, true).tone, 'live')
  }
  assert.notEqual(sessionBanner(launch, { ...runtime, llm: { configured: false } }, true, true).tone, 'live')
  assert.equal(sessionBanner(launch, { ...runtime, preferences: { commentary_paused: true } }, true, true).title, 'Following quietly')
})
