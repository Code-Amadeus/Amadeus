import assert from 'node:assert/strict'
import test from 'node:test'
import { advanceSpokenCaption } from '../src/renderer/components/spokenCaptionState.ts'
const start = (id, text, turn_id = 'one') => ({ sentence_id: id, text, turn_id })
test('caption follows actual playback, combines its sentences and resets for a new notification', () => {
  let c = advanceSpokenCaption(null, 'companion.voice', { status: 'accepted', text: 'unplayed candidate' })
  assert.equal(c, null)
  c = advanceSpokenCaption(c, 'tts.sentence_start', start('a', '確認できたわ。'))
  c = advanceSpokenCaption(c, 'tts.sentence_start', start('b', '結果を見て。'))
  assert.equal(c.text, '確認できたわ。結果を見て。')
  c = advanceSpokenCaption(c, 'tts.sentence_start', start('b', '結果を見て。'))
  assert.equal(c.text, '確認できたわ。結果を見て。')
  c = advanceSpokenCaption(c, 'tts.status', { status: 'interrupted' })
  assert.equal(c.status, 'interrupted')
  c = advanceSpokenCaption(c, 'tts.sentence_start', start('c', '次の話よ。', 'two'))
  assert.equal(c.text, '次の話よ。')
  assert.equal(advanceSpokenCaption(c, 'tts.turn_complete', {}).status, 'finished')
})
