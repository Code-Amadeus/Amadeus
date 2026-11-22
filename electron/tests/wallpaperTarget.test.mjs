import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import test from 'node:test'
import {
  enumerateWindowsWallpaperMonitors,
  parseWallpaperMonitors,
  rectsMatchWithin,
  resolveWallpaperTargetDisplay,
  wallpaperMonitorOptions,
  wallpaperMonitorTarget,
  wallpaperTargetProblem,
  windowsWallpaperHelperScript,
} from '../src/main/wallpaperTarget.ts'

// The shared identity is the Windows GDI device name reported by the helper's
// `screens` command; Electron displays only ever contribute labels and bounds.
const primary = { monitor: '\\\\.\\DISPLAY1', x: 0, y: 0, width: 1920, height: 1080, primary: true }
const secondary = { monitor: '\\\\.\\DISPLAY2', x: 1920, y: 0, width: 2560, height: 1440, primary: false }
const left = { monitor: '\\\\.\\DISPLAY3', x: -1600, y: 0, width: 1600, height: 900, primary: false }
const monitors = [primary, secondary, left]
const wire = monitors.map(m => ({
  Monitor: m.monitor, X: m.x, Y: m.y, Width: m.width, Height: m.height, Primary: m.primary,
}))

// Electron reports DIP bounds; screen.dipToScreenRect scales by the display's
// own factor, recovering its physical rectangle within one pixel of rounding.
const scale = factor => ({ x, y, width, height }) => ({
  x: Math.round(x * factor), y: Math.round(y * factor),
  width: Math.round(width * factor), height: Math.round(height * factor),
})
const dip = factor => ({ x, y, width, height }) => ({
  x: Math.round(x / factor), y: Math.round(y / factor),
  width: Math.round(width / factor), height: Math.round(height / factor),
})
const displays = [
  { id: 1, label: 'Main', scale: 1, dipBounds: dip(1)(primary) },
  { id: 2, label: 'Right', scale: 1.5, dipBounds: dip(1.5)(secondary) },
  { id: 3, label: 'Left', scale: 1, dipBounds: dip(1)(left) },
]
const physicalOf = display => scale(display.scale)(display.dipBounds)

test('blank or absent choices keep the historical primary-display behavior', () => {
  assert.equal(wallpaperMonitorTarget(undefined), undefined)
  assert.equal(wallpaperMonitorTarget(''), undefined)
  assert.equal(wallpaperMonitorTarget('   '), undefined)
  assert.equal(resolveWallpaperTargetDisplay(undefined, monitors, displays, physicalOf).status, 'default')
  assert.equal(resolveWallpaperTargetDisplay('', monitors, displays, physicalOf).status, 'default')
})

test('an explicit choice resolves to exactly one Electron display', () => {
  const target = wallpaperMonitorTarget(' \\\\.\\DISPLAY2 ')
  const resolution = resolveWallpaperTargetDisplay(target, monitors, displays, physicalOf)
  assert.equal(resolution.status, 'resolved')
  assert.equal(resolution.display.id, 2)
  assert.equal(resolution.descriptor.monitor, '\\\\.\\DISPLAY2')
})

test('negative display origins participate in correlation', () => {
  const resolution = resolveWallpaperTargetDisplay('\\\\.\\DISPLAY3', monitors, displays, physicalOf)
  assert.equal(resolution.status, 'resolved')
  assert.equal(resolution.display.id, 3)
})

test('a disconnected or ambiguous target reports without choosing a display', () => {
  for (const [monitorList, displayList, target, status] of [
    [[primary, left], displays, '\\\\.\\DISPLAY2', 'disconnected'],
    [[primary, { ...primary, monitor: '\\\\.\\DISPLAY9' }, { ...primary, monitor: '\\\\.\\DISPLAY9' }],
      displays, '\\\\.\\DISPLAY9', 'ambiguous'],
    [[primary, { ...secondary, x: 3840 }], displays, '\\\\.\\DISPLAY2', 'unmatched'],
    [[primary, secondary, { ...secondary, monitor: '\\\\.\\DISPLAY4' }],
      [...displays, { id: 4, label: 'Dup', scale: 1.5, dipBounds: dip(1.5)(secondary) }],
      '\\\\.\\DISPLAY4', 'ambiguous'],
  ]) {
    const resolution = resolveWallpaperTargetDisplay(target, monitorList, displayList, physicalOf)
    assert.equal(resolution.status, status)
    assert.ok(wallpaperTargetProblem(status, target).includes('DISPLAY'))
    assert.ok(!('display' in resolution))
  }
})

test('physical conversion absorbs one-pixel DIP rounding in both directions', () => {
  assert.equal(rectsMatchWithin(scale(1.5)(dip(1.5)(secondary)), secondary), true)
  assert.equal(rectsMatchWithin({ x: 0, y: 0, width: 2560, height: 1440 }, secondary), false)
  assert.equal(rectsMatchWithin({ ...secondary, x: secondary.x + 2 }, secondary), false)
})

test('parseWallpaperMonitors rejects missing or malformed entries', () => {
  assert.deepEqual(parseWallpaperMonitors({ monitors: wire }), monitors)
  for (const bad of [{}, { monitors: null }, { monitors: [{ Monitor: '' }] },
    { monitors: [{ Monitor: '\\\\.\\DISPLAY1', X: 0, Y: 0, Width: 0, Height: 1080 }] }]) {
    assert.throws(() => parseWallpaperMonitors(bad))
  }
})

test('monitor options label each device name once and mark the primary', () => {
  const options = wallpaperMonitorOptions(monitors, displays, physicalOf)
  assert.equal(options.length, 3)
  assert.deepEqual(options.map(o => o.monitor), monitors.map(m => m.monitor))
  assert.match(options[0].label, /DISPLAY1/)
  assert.match(options[0].label, /Primary/)
  assert.match(options[1].label, /DISPLAY2/)
  assert.match(options[1].label, /2560×1440/)
  assert.doesNotMatch(options[2].label, /Primary/)
})

function fakeSpawn(lines) {
  return () => {
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    queueMicrotask(() => queueMicrotask(() => {
      for (const line of lines) child.stdout.emit('data', line)
      child.emit('exit', 0)
    }))
    return child
  }
}

test('enumeration parses the helper screens report', async () => {
  const parsed = await enumerateWindowsWallpaperMonitors('python', 'helper.py',
    fakeSpawn([JSON.stringify({ monitors: wire })]))
  assert.deepEqual(parsed, monitors)
})

test('helper script path follows packaged resources layout', () => {
  assert.equal(windowsWallpaperHelperScript('root', 'res', false),
    path.join('root', 'electron', 'resources', 'windows-wallpaper', 'helper.py'))
  assert.equal(windowsWallpaperHelperScript('root', 'res', true),
    path.join('res', 'windows-wallpaper', 'helper.py'))
})
