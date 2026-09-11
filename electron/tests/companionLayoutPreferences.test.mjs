import assert from 'node:assert/strict'
import test from 'node:test'
import { freshLayoutPreferences, freshLayoutProfile, saveLayoutProfile, readLayoutPreferences,
  taskLayoutOffset, scenePoint } from '../src/renderer/components/companionLayoutPreferences.ts'

test('mode and monitor profiles survive reopening without persisting an edit lock', () => {
  let prefs = freshLayoutPreferences()
  const manual = { ...freshLayoutProfile(), scene: { x: 20, y: -50, scale: .8 }, offsets: { projects: { p: { x: 22, y: 33 } }, tasks: {} } }
  prefs = saveLayoutProfile(prefs, 'secondary:1080x1872@1', 'natural', manual)
  prefs = saveLayoutProfile(prefs, 'secondary:1080x1872@1', 'ordered', freshLayoutProfile())
  prefs = saveLayoutProfile(prefs, 'primary:480x820@1.5', 'natural', freshLayoutProfile())
  const restored = readLayoutPreferences(JSON.stringify({ ...prefs, mode: 'scattered' }))
  assert.deepEqual(restored.displays['secondary:1080x1872@1'].natural, manual)
  assert.deepEqual(restored.displays['secondary:1080x1872@1'].ordered, freshLayoutProfile())
  assert.equal(restored.edit, undefined)
  assert.deepEqual(readLayoutPreferences('broken JSON'), freshLayoutPreferences())
})

test('legacy saved task offsets remain readable for descendants and new children', () => {
  const tasks = [{ id: 'other' }, { id: 'parent' }, { id: 'side', parentTaskId: 'parent' }, { id: 'grandchild', parentTaskId: 'side' }]
  const offsets = { projects: {}, tasks: { parent: { x: 35, y: 62 }, side: { x: -11, y: 8 } } }
  assert.deepEqual(taskLayoutOffset(tasks[0], tasks, offsets), { x: 0, y: 0 })
  assert.deepEqual(taskLayoutOffset(tasks[1], tasks, offsets), { x: 35, y: 62 })
  assert.deepEqual(taskLayoutOffset(tasks[3], tasks, offsets), { x: 24, y: 70 })
  const added = { id: 'new', parentTaskId: 'parent' }
  assert.deepEqual(taskLayoutOffset(added, [...tasks, added], offsets), { x: 35, y: 62 })
  assert.equal(tasks[2].parentTaskId, 'parent')
})

test('scene transform preserves the relative composition', () => {
  for (const scale of [.6, .85, 1, 1.2]) {
    const scene = { x: 80, y: -90, scale }, a = { x: 40, y: 60 }, b = { x: 500, y: 990 }
    const aa = scenePoint(a, scene, 1080, 1872), bb = scenePoint(b, scene, 1080, 1872)
    assert.ok(Math.abs((bb.x - aa.x) - (b.x - a.x) * scale) < 1e-8)
    assert.ok(Math.abs((bb.y - aa.y) - (b.y - a.y) * scale) < 1e-8)
  }
})
