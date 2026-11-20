import assert from 'node:assert/strict'
import test from 'node:test'
import path from 'node:path'
import { auipStoragePartition } from '../src/main/auipStorage.ts'

test('reopening an amended Work entry retains its app data namespace', () => {
  const entry = path.resolve('draft/notes/index.html')
  const first = auipStoragePartition('work-notes', entry)
  assert.equal(auipStoragePartition('work-notes', path.join(path.dirname(entry), '.', 'index.html')), first)
  assert.match(first, /^persist:auip-work-[a-f0-9]{64}$/)
  if (process.platform === 'win32') {
    assert.equal(auipStoragePartition('work-notes', entry.toUpperCase()), first)
  }
})

test('another Work or another app entry cannot inherit saved notes', () => {
  const entry = path.resolve('draft/notes/index.html')
  const first = auipStoragePartition('work-notes', entry)
  assert.notEqual(auipStoragePartition('work-recipe', entry), first)
  assert.notEqual(auipStoragePartition('work-notes', path.resolve('draft/notes/other.html')), first)
})
