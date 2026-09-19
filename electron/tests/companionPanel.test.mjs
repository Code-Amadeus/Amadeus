import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { dockPanel, clampPanel } from '../src/main/companionPanelLayout.ts'
import { companionPortraitStatus, readCompanionPortraits } from '../src/main/companionPortraits.ts'

const overlap = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
test('docking reserves disjoint game/card space on wide, narrow and negative-origin monitors', () => {
  for (const area of [{ x: 0, y: 0, width: 1920, height: 1040 }, { x: 0, y: 0, width: 1024, height: 728 }, { x: -1920, y: 50, width: 1920, height: 1080 }]) {
    const { game, panel } = dockPanel({ ...area }, area)
    assert.equal(overlap(game, panel), false)
    assert.deepEqual(clampPanel(game, area), game)
    assert.deepEqual(clampPanel(panel, area), panel)
  }
})
test('docking uses existing space on either side without resizing the game', () => {
  const area = { x: 0, y: 0, width: 1920, height: 1080 }
  for (const x of [20, 850]) {
    const before = { x, y: 100, width: 900, height: 700 }
    const { game, panel } = dockPanel(before, area)
    assert.deepEqual(game, before)
    assert.equal(overlap(game, panel), false)
  }
})
test('optional VN cache reuses frames but cannot read outside its root', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'companion-'))
  try {
    await fs.mkdir(path.join(root, 'cache'))
    await fs.writeFile(path.join(root, 'outside.png'), 'private')
    await fs.writeFile(path.join(root, 'cache', 'face.png'), 'portrait')
    await fs.writeFile(path.join(root, 'cache', 'manifest.json'), JSON.stringify({ emotions: {
      normal: { idle: ['face.png', 'missing.png', '../outside.png'], speaking: ['face.png'] },
    } }))
    const frames = await readCompanionPortraits(path.join(root, 'cache'))
    assert.equal(frames.normal.idle.length, 1)
    assert.equal(frames.normal.speaking.length, 1)
    assert.equal(Buffer.from(frames.normal.idle[0].split(',')[1], 'base64').toString(), 'portrait')
    assert.deepEqual(await readCompanionPortraits(path.join(root, 'missing')), {})
    const status = await companionPortraitStatus(path.join(root, 'cache'))
    assert.equal(status.state, 'incomplete')
    assert.equal(status.emotionCount, 1)
    assert.equal(status.frameCount, 1)
    assert.equal((await companionPortraitStatus(path.join(root, 'missing'))).state, 'not_installed')
    await fs.writeFile(path.join(root, 'cache', 'manifest.json'), JSON.stringify({ emotions: {
      normal: { idle: ['face.png'], speaking: ['face.png'] },
    } }))
    assert.equal((await companionPortraitStatus(path.join(root, 'cache'))).state, 'ready')
  } finally { await fs.rm(root, { recursive: true, force: true }) }
})
test('companion projects the shared current display without acting on Work or AUIP events', async () => {
  const scope = vm.createContext({})
  vm.runInContext(await fs.readFile(new URL('../../render/web/companion_presentation.js', import.meta.url), 'utf8'), scope)
  const apply = scope.CompanionPresentation.apply
  let state = { text: '', speaking: false, emotion: 'normal' }
  state = apply(state, { method: 'setSubtitle', args: ['按笔记上的线索想一想。'] })
  state = apply(state, { method: 'setEmotion', args: ['thinking'] })
  state = apply(state, { method: 'setSpeaking', args: [true] })
  assert.equal(state.emotion, 'sided_thinking')
  assert.equal(state.speaking, true)
  assert.equal(apply(state, { method: 'triggerSpriteForgeIntent', args: ['trans_smile'] }).emotion, 'happy')
  assert.strictEqual(apply(state, { method: 'setCanvas', args: [{ text: 'provider output' }] }), state)
  state = apply(state, { method: 'setSpeaking', args: [false] })
  assert.equal(state.text, '按笔记上的线索想一想。')
  assert.equal(state.speaking, false)
  assert.strictEqual(apply(state, { method: 'setSubtitle', args: [''] }), state)
  assert.strictEqual(apply(state, { method: 'setSubtitle', args: ['   '] }), state)
  assert.equal(apply(state, { method: 'setSubtitle', args: ['下一句。'] }).text, '下一句。')
})
test('speech completion keeps the last line in an open card regardless of clear/stop order', async () => {
  const scope = vm.createContext({})
  vm.runInContext(await fs.readFile(new URL('../../render/web/companion_presentation.js', import.meta.url), 'utf8'), scope)
  const apply = scope.CompanionPresentation.apply
  const stop = { method: 'setSpeaking', args: [false] }
  const clear = { method: 'setSubtitle', args: [''] }
  for (const ending of [[stop, clear], [clear, stop]]) {
    let state = { text: '', speaking: false, emotion: 'normal' }
    for (const line of ['第一句说完后留在这里。', '下一句说完也不跳回欢迎语。']) {
      state = apply(state, { method: 'setSubtitle', args: [line] })
      state = apply(state, { method: 'setSpeaking', args: [true] })
      for (const event of ending) state = apply(state, event)
      state = apply(state, { method: 'setEmotion', args: ['normal'] })
      assert.equal(state.text, line)
      assert.equal(state.speaking, false)
    }
  }
})
test('suppression restores the exact renderable state and leaves scenario visibility alone', async () => {
  const sprite = { renderable: true, visible: false }
  const live2d = { renderable: false, visible: true }
  const subtitle = { renderable: true, visible: true }
  const window = { renderApp: { _sprite: { container: sprite }, _live2d: { container: live2d }, _subtitle: { container: subtitle } } }
  const scope = vm.createContext({ window, console, URLSearchParams })
  vm.runInContext(await fs.readFile(new URL('../../render/web/wallpaper_scene.js', import.meta.url), 'utf8'), scope)
  window.wallpaperApp.setCompanionActive(true)
  window.wallpaperApp.setCompanionActive(true)
  assert.equal(sprite.renderable, false)
  assert.equal(subtitle.renderable, false)
  assert.equal(live2d.visible, true)
  window.wallpaperApp.setCompanionActive(false)
  assert.equal(sprite.renderable, true)
  assert.equal(sprite.visible, false)
  assert.equal(live2d.renderable, false)
  assert.equal(subtitle.renderable, true)
})
