import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveCompanionDesktop } from '../src/main/windowPlacement.ts'
import { boundsOf, displayForPoint, fitCarriedScene, inheritedPlacement, localWorkArea, nodeKey, nodesOnDisplay,
  placeSelection, recoverNodeDelta, transformSceneNode } from '../src/renderer/components/companionDesktopLayout.ts'
import { freshLayoutPreferences, freshLayoutProfile, readLayoutPreferences, saveLayoutProfile } from '../src/renderer/components/companionLayoutPreferences.ts'

const primary = { id: 1, bounds: { x: 0, y: 0, width: 2560, height: 1440 }, workArea: { x: 0, y: 0, width: 2560, height: 1392 }, scaleFactor: 1.5 }
const secondary = { id: 2, bounds: { x: 2560, y: 0, width: 1080, height: 1920 }, workArea: { x: 2560, y: 0, width: 1080, height: 1872 }, scaleFactor: 1 }
const desktop = resolveCompanionDesktop([primary, secondary], primary)
const scene = { x: 0, y: 0, scale: 1 }
const nodes = [
  { id: 'project', kind: 'projects', x: 400, y: 40, width: 280, height: 36, scale: 1 },
  { id: 'source', kind: 'tasks', x: 500, y: 110, width: 300, height: 184, scale: 1 },
  { id: 'resident', kind: 'tasks', x: -2000, y: 100, width: 300, height: 184, scale: 1 },
]
const geometry = node => ({ x: node.x, y: node.y, scale: node.scale })

test('the native surface covers all displays and the home frame preserves the existing portrait profile', () => {
  assert.deepEqual(desktop.bounds, { x: 0, y: 0, width: 3640, height: 1920 })
  assert.deepEqual(desktop.home, secondary.workArea)
  assert.equal(desktop.legacyKey, '2:1080x1872:1080x1872@1')
  assert.equal(resolveCompanionDesktop([secondary, primary], primary).key, desktop.key)
  assert.notEqual(resolveCompanionDesktop([{ ...primary, scaleFactor: 1 }, secondary], primary).key, desktop.key)
})

test('left and upper monitors use signed DIP coordinates without applying DPI twice', () => {
  const left = { id: 3, bounds: { x: -1600, y: -200, width: 1600, height: 900 }, scaleFactor: 1.25 }
  const all = resolveCompanionDesktop([primary, secondary, left], primary)
  assert.deepEqual(all.bounds, { x: -1600, y: -200, width: 5240, height: 2120 })
  assert.equal(displayForPoint({ x: -4000, y: -100 }, all).id, 3)
  assert.equal(displayForPoint({ x: -1500, y: 100 }, all).id, 1)
})

test('moving the character carries source-screen cards but leaves a resident card exactly unchanged', () => {
  const selected = nodesOnDisplay(nodes, 2, desktop)
  assert.deepEqual([...selected].sort(), ['projects:project', 'tasks:source'])
  const after = { x: -1400, y: -300, scale: 1 }
  const result = placeSelection(undefined, nodes, selected, node => transformSceneNode(node, scene, after, 1080, 1872))
  assert.deepEqual(result.tasks.resident, geometry(nodes[2]))
  assert.deepEqual(result.tasks.source, { x: -900, y: -190, scale: 1 })
  assert.deepEqual(result.projects.project, { x: -1000, y: -260, scale: 1 })
})

test('a card crosses independently without relocating the project or the character transform', () => {
  const selected = new Set(['tasks:source'])
  const result = placeSelection(undefined, nodes, selected, node => ({ ...node, x: node.x - 1500 }))
  assert.equal(displayForPoint({ ...result.tasks.source, y: 200 }, desktop).id, 1)
  assert.deepEqual(result.projects.project, geometry(nodes[0]))
  assert.deepEqual(result.tasks.resident, geometry(nodes[2]))
  assert.deepEqual(scene, { x: 0, y: 0, scale: 1 })
})

test('new descendants inherit the placed parent, while an explicitly detached descendant stays independent', () => {
  const parent = { id: 'p', x: 100, y: 100, width: 300, height: 184, scale: 1 }
  const child = { id: 'c', x: 140, y: 310, width: 300, height: 184, scale: 1 }
  const placements = { projects: {}, tasks: { p: { x: -900, y: 80, scale: .8 } } }
  const inherited = inheritedPlacement(child, 'tasks', placements, [{ kind: 'tasks', pose: parent }])
  assert.deepEqual(geometry(inherited), { x: -868, y: 248, scale: .8 })
  placements.tasks.c = { x: 420, y: 500, scale: 1 }
  assert.deepEqual(geometry(inheritedPlacement(child, 'tasks', placements, [{ kind: 'tasks', pose: parent }])), placements.tasks.c)
})

test('dropping a portrait composition on the landscape screen fits only that carried group', () => {
  const selected = nodesOnDisplay(nodes, 2, desktop)
  const character = { id: 'character', kind: 'tasks', x: 200, y: 810, width: 680, height: 1060, scale: 1 }
  const deltaScene = { x: -1300, y: -300, scale: 1 }
  const carried = [...nodes.filter(node => selected.has(nodeKey(node))), character]
  const moved = carried.map(node => ({ ...node, ...transformSceneNode(node, scene, deltaScene, 1080, 1872) }))
  const target = localWorkArea(primary, desktop)
  const fit = fitCarriedScene(deltaScene, boundsOf(moved), target, 1080, 1872)
  assert.ok(fit.scale < 1)
  const fitted = boundsOf(carried.map(node => ({ ...node, ...transformSceneNode(node, scene, fit, 1080, 1872) })))
  assert.ok(fitted.x >= target.x + 11 && fitted.y >= 11)
  assert.ok(fitted.x + fitted.width <= target.x + target.width - 11)
  assert.ok(fitted.y + fitted.height <= target.height - 11)
  const saved = placeSelection(undefined, nodes, selected, node => transformSceneNode(node, scene, fit, 1080, 1872))
  assert.deepEqual(saved.tasks.resident, geometry(nodes[2]))
})

test('drops in a desktop gap recover to the destination work area', () => {
  const delta = recoverNodeDelta([nodes[1]], { x: -3500, y: -600 }, localWorkArea(primary, desktop))
  const moved = { ...nodes[1], x: nodes[1].x + delta.x, y: nodes[1].y + delta.y }
  assert.ok(moved.x >= -2560 && moved.y >= 0)
  assert.ok(moved.x + moved.width <= 0 && moved.y + moved.height <= 1392)
})

test('cross-screen placement survives reopen and disconnect without overwriting the original topology', () => {
  let prefs = freshLayoutPreferences()
  const profile = { ...freshLayoutProfile(), placements: placeSelection(undefined, nodes, new Set(), node => node) }
  prefs = saveLayoutProfile(prefs, desktop.key, 'natural', profile)
  const detached = resolveCompanionDesktop([primary], primary)
  prefs = saveLayoutProfile(prefs, detached.key, 'natural', freshLayoutProfile())
  assert.deepEqual(readLayoutPreferences(JSON.stringify(prefs)).displays[desktop.key].natural, profile)
  assert.notEqual(detached.key, desktop.key)
})
