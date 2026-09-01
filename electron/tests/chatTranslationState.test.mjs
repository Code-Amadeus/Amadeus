import assert from 'node:assert/strict'
import test from 'node:test'

import {
  chatTranslationCandidates,
  chatTranslationKey,
} from '../src/renderer/components/chatTranslationState.ts'

test('translation candidates contain only completed assistant display text', () => {
  const messages = [
    { role: 'user', text: '続けて' },
    { role: 'assistant', text: '今から確認するわ。', turnId: 'turn-1', streaming: true },
    { role: 'assistant', text: '確認が終わったわ。', turnId: 'turn-2', streaming: false },
    { role: 'system', text: 'backend ready' },
  ]

  assert.deepEqual(chatTranslationCandidates(messages), [{
    key: chatTranslationKey(messages[2], 2),
    text: '確認が終わったわ。',
    turnId: 'turn-2',
  }])
})

test('translation identity changes with visible text without mutating history messages', () => {
  const original = { role: 'assistant', text: '確認中よ。', turnId: 'turn-1', streaming: false }
  const messages = [original]
  const before = structuredClone(messages)
  const firstKey = chatTranslationKey(original, 0)
  const nextKey = chatTranslationKey({ ...original, text: '確認が終わったわ。' }, 0)

  assert.notEqual(firstKey, nextKey)
  assert.deepEqual(messages, before)
  assert.equal('translation' in original, false)
})
