import assert from 'node:assert/strict'
import test from 'node:test'
import { canLeaveSettingsEditors } from '../src/renderer/components/settingsDraft.ts'

test('untouched editors allow navigation without confirmation', () => {
  assert.equal(canLeaveSettingsEditors([{ dirty: false, busy: false }], () => assert.fail('unexpected prompt')), true)
})

test('declining discard preserves the current editing context', () => {
  assert.equal(canLeaveSettingsEditors([{ dirty: true, busy: false }], () => false), false)
})

test('one confirmation covers all unsaved connection editors', () => {
  let prompts = 0
  const states = [{ dirty: true, busy: false }, { dirty: true, busy: false }]
  assert.equal(canLeaveSettingsEditors(states, () => { prompts++; return true }), true)
  assert.equal(prompts, 1)
})

test('a pending write cannot be abandoned even if another editor is clean', () => {
  for (const dirty of [false, true]) {
    assert.equal(canLeaveSettingsEditors([{ dirty: false, busy: false }, { dirty, busy: true }],
      () => assert.fail('a pending write is not discardable')), false)
  }
})
