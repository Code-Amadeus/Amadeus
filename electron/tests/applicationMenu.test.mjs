import assert from 'node:assert/strict'
import test from 'node:test'

import { applicationMenuTemplate } from '../src/main/applicationMenu.ts'

test('macOS keeps the native edit menu for clipboard shortcuts', () => {
  assert.deepEqual(applicationMenuTemplate('darwin'), [
    { role: 'appMenu' },
    { role: 'editMenu' },
  ])
})

test('other desktop platforms keep the application menu hidden', () => {
  assert.equal(applicationMenuTemplate('win32'), null)
  assert.equal(applicationMenuTemplate('linux'), null)
})
