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

test('tall captions preserve usable preview space when stacked and remain inside the work area', () => {
  for (const area of [{ x: 0, y: 0, width: 1024, height: 728 }, { x: -1024, y: 50, width: 1024, height: 1000 }]) {
    const initial = dockPanel({ ...area }, area, 470, 226, 520)
    const grown = dockPanel(initial.game, area, 470, 2000, 520)
    assert.ok(grown.game.height >= Math.min(520, initial.game.height))
    assert.equal(overlap(grown.game, grown.panel), false)
    assert.deepEqual(clampPanel(grown.panel, area), grown.panel)
    assert.deepEqual(clampPanel(grown.game, area), grown.game)
    assert.ok(grown.panel.height >= 226)
    if (area.height === 1000) assert.ok(grown.panel.height > 226, 'spare height remains available for captions')
    assert.deepEqual(dockPanel(grown.game, area, 470, 226, 520), initial, 'short captions restore the original stacked layout')
  }
  const area = { x: 0, y: 0, width: 1920, height: 1040 }
  const wide = dockPanel({ ...area }, area, 470, 2000, 520)
  assert.equal(wide.panel.height, area.height, 'side-by-side cards can use the full work area height')
  assert.equal(wide.game.height, area.height)
  assert.equal(overlap(wide.game, wide.panel), false)
  const offsetGame = { x: 30, y: 100, width: 1600, height: 700 }
  const offset = dockPanel(offsetGame, area, 470, 2000, 520)
  assert.deepEqual(clampPanel(offset.panel, area), offset.panel, 'a tall side card is clamped even when the preview is offset vertically')
  assert.equal(offset.game.y, offsetGame.y)
  assert.equal(offset.game.height, offsetGame.height)
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
test('default Companion Lite atlas reports installed WebP resources', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'companion-atlas-'))
  try {
    const clip = Buffer.from('webp-atlas')
    await fs.mkdir(path.join(root, 'normal'))
    await fs.writeFile(path.join(root, 'normal', 'idle.webp'), clip)
    await fs.writeFile(path.join(root, 'manifest.json'), JSON.stringify({
      format: 'amadeus.companion-atlas.v1',
      emotions: {
        normal: {
          idle: { url: 'normal/idle.webp', sequence: [0, 1], fileBytes: clip.length },
          speaking: { url: 'normal/idle.webp', sequence: [0, 1, 2], fileBytes: clip.length },
        },
      },
    }))
    const status = await companionPortraitStatus(root)
    assert.equal(status.state, 'ready')
    assert.equal(status.installed, true)
    assert.equal(status.emotionCount, 1)
    assert.equal(status.frameCount, 5)
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
  assert.equal(apply(state, { method: 'triggerCharacterIntent', args: ['trans_smile'] }).emotion, 'happy')
  assert.equal(apply(state, { method: 'triggerCharacterIntent', args: ['smile', { backend: 'live2d', semantic_label: 'smile' }] }).emotion, 'happy')
  assert.equal(apply(state, { method: 'triggerCharacterIntent', args: ['work', { backend: 'live2d', semantic_label: 'work' }] }).emotion, 'sided_thinking')
  assert.equal(apply(state, { method: 'triggerCharacterIntent', args: ['happy', { backend: 'live2d', semantic_label: 'happy' }] }).emotion, 'happy')
  const canonical = ['normal', 'sided_thinking', 'sided_surprised', 'happy', 'blush', 'angry', 'sad', 'disappointed']
  for (const emotion of ['normal', 'angry', 'sad', 'blush', 'disappointed']) {
    assert.equal(apply(state, { method: 'triggerCharacterIntent', args: [emotion, { backend: 'live2d', semantic_label: emotion }] }, canonical).emotion, emotion)
  }
  assert.strictEqual(apply(state, { method: 'triggerCharacterIntent', args: ['unknown-emotion', { backend: 'live2d', semantic_label: 'unknown-emotion' }] }, canonical), state)
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
  const window = { renderApp: { _sprite: { container: sprite }, getModelCharacterContainer: () => live2d, _subtitle: { container: subtitle } } }
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
