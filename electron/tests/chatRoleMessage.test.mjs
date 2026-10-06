import assert from 'node:assert/strict'
import test from 'node:test'
import {
  acceptedRoleMessage,
  applyRoleMessage,
  assistantTurnAnchors,
  patchInterruptedMessage,
  finishAssistantTurn,
  updateAssistantMessage,
  chatAsrDestination,
  chatEventMatchesSession,
  runSessionSelection,
} from '../src/renderer/components/chatMessageState.ts'

test('independent head settles its streamed row and keeps a later Host reply separate', () => {
  let rows = updateAssistantMessage([], 'turn', 'Head.', true)
  const head = acceptedRoleMessage({ session_id: 'A', message_id: 'head', turn_id: 'turn', text: 'Head.' }, 'A', new Set())
  rows = applyRoleMessage(rows, head)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].messageId, 'head')
  assert.equal(rows[0].streaming, false)
  assert.deepEqual(finishAssistantTurn(rows, 'turn', 'Head.'), rows)
  rows = applyRoleMessage(rows, { messageId: 'turn', text: 'Accepted Work.' })
  assert.deepEqual(rows.map(row => row.text), ['Head.', 'Accepted Work.'])
  assert.deepEqual(finishAssistantTurn(rows, 'turn', 'Accepted Work.'), rows)
  assert.equal(acceptedRoleMessage({ session_id: 'A', message_id: 'head', turn_id: 'turn', text: 'Head.' }, 'A', new Set(['turn'])), null)
})

test('merged role text replaces the stream once while legacy completion still works', () => {
  const streamed = updateAssistantMessage([], 'turn', 'Head.', true)
  const merged = applyRoleMessage(streamed, { messageId: 'turn', text: 'Head.Tail.' })
  assert.equal(merged.length, 1)
  assert.deepEqual(finishAssistantTurn(merged, 'turn', 'Head.Tail.'), merged)
  assert.equal(finishAssistantTurn(streamed, 'turn', 'Legacy final.')[0].text, 'Legacy final.')
})

test('Work cards and interruption attach to the latest message of a multi-message turn', () => {
  const head = applyRoleMessage([], { messageId: 'head', turnId: 'turn', text: 'Head.' })
  const rows = updateAssistantMessage(head, 'turn', 'Remote partial', true)
  assert.equal(assistantTurnAnchors(rows).get('turn'), 1)
  const interrupted = patchInterruptedMessage(rows, { turnId: 'turn', text: 'Remote partial' })
  assert.equal(interrupted[0].text, 'Head.')
  assert.match(interrupted[1].text, /interrupted by user/)
})

test('role text is accepted only by its current, non-interrupted conversation', () => {
  const payload = { session_id: 'A', message_id: 'run', text: 'What should I record?' }
  assert.deepEqual(acceptedRoleMessage(payload, 'A', new Set()), {
    messageId: 'run', text: payload.text,
  })
  assert.equal(acceptedRoleMessage(payload, 'B', new Set()), null)
  assert.equal(acceptedRoleMessage(payload, 'A', new Set(['run'])), null)
  assert.equal(acceptedRoleMessage({ ...payload, message_id: '' }, 'A', new Set()), null)
})

test('ASR keeps wallpaper direct-send separate from the ChatPage composer', () => {
  assert.equal(chatAsrDestination('wake'), 'direct')
  assert.equal(chatAsrDestination('vn_player'), 'ignore')
  assert.equal(chatAsrDestination(''), 'composer')
  assert.equal(chatAsrDestination(undefined), 'composer')
})

test('a delayed New chat keeps sending blocked until the new Session payload is applied', async () => {
  const selection = { pending: 0 }
  let activeSession = 'old'
  let messages = ['old conversation']
  const pendingChanges = []
  let resolve
  const response = new Promise(done => { resolve = done })
  const operation = runSessionSelection(selection, pending => {
    pendingChanges.push(pending)
    if (!pending) assert.equal(activeSession, 'new')
  }, () => response, payload => {
    activeSession = payload.current_session_id
    messages = payload.messages
  })
  assert.equal(selection.pending, 1) // Immediately, before React can rerender.
  assert.equal(activeSession, 'old')
  await Promise.resolve()
  assert.equal(selection.pending, 1) // A stalled backend is still a pending selection.
  resolve({ ok: true, current_session_id: 'new', messages: [] })
  await operation
  assert.equal(selection.pending, 0)
  assert.deepEqual(messages, [])
  assert.deepEqual(pendingChanges, [true, false])
  assert.equal(chatEventMatchesSession({ session_id: 'old', turn_id: 'old-turn' },
    activeSession, new Set()), false)
})

test('overlapping selections and failed requests cannot release another pending selection', async () => {
  const selection = { pending: 0 }
  let resolve
  const first = runSessionSelection(selection, () => {}, () => new Promise(done => { resolve = done }), () => {})
  const second = runSessionSelection(selection, () => {}, async () => { throw Error('offline') }, () => {
    assert.fail('failed selection must not replace the current conversation')
  })
  await assert.rejects(second, /offline/)
  assert.equal(selection.pending, 1)
  resolve({ ok: false })
  await first
  assert.equal(selection.pending, 0)
})

test('token, completion and error keep Session ownership for local, passive-window and voice turns', () => {
  for (const turn_id of ['local-turn', 'other-window-turn', 'wake-turn']) {
    const payload = { session_id: 'A', turn_id }
    assert.equal(chatEventMatchesSession(payload, 'A', new Set()), true)
    assert.equal(chatEventMatchesSession(payload, 'B', new Set()), false)
    assert.equal(chatEventMatchesSession(payload, 'A', new Set([turn_id])), false)
  }
  assert.equal(chatEventMatchesSession({ turn_id: 'unknown-session' }, 'A', new Set()), false)
  assert.equal(chatEventMatchesSession({ session_id: 'A' }, 'A', new Set()), false)
})
