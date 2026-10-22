import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const { validateThemePalette, parseThemeColor } = loadTypeScript(new URL('../src/shared/extensionTheme.ts', import.meta.url))
const { createThemeApplication } = loadTypeScript(new URL('../src/renderer/themeApplication.ts', import.meta.url))
const theme = (base = 'classic', tokens = {}) => ({ id: 'palette', contract: 'amadeus.ui.theme/v0', base, tokens: { ...(base === 'classic' ? { '--faint': '#667085', '--selection-bg': '#F2F4F7', '--subtle-fill': '#F2F4F7', '--selected-fill': '#EAECF0', '--focus-fill': '#F2F4F7' } : { '--faint': '#95C5BA', '--accent': '#7DE0CC', '--accent-hover': '#62BDA8', '--selection-bg': 'rgba(84,255,221,.10)' }), ...tokens } })

test('both built-in identities remain usable; only literal token overrides are accepted', () => {
  for (const base of ['classic', 'wallpaper-slice']) {
    const palette = validateThemePalette(theme(base))
    assert.equal(palette.base, base)
    assert.equal(parseThemeColor(palette.tokens['--bg'])[3], 1)
  }
  assert.deepEqual(parseThemeColor(validateThemePalette(theme('classic', { '--bg': '#0000' })).tokens['--bg']),
    parseThemeColor(validateThemePalette(theme('classic')).tokens['--bg']))
  assert.deepEqual(parseThemeColor('#fff'), parseThemeColor('hsl(180, 0%, 100%)'))
  assert.deepEqual(parseThemeColor('#ff000080'), parseThemeColor(`rgba(255,0,0,${128 / 255})`))
  for (const tokens of [{ '--danger': '#000' }, { '--text': '#fff' }, { '--bg': 'url(file:///outside)' }, { '--bg': 'var(--bg)' }, { '--font': 'Arial; color:red' }, { '--not-a-token': '#000' }, { '--accent': '#fff' }]) {
    assert.throws(() => validateThemePalette(theme('classic', tokens)), /Theme tokens/)
  }
  assert.throws(() => validateThemePalette(theme('classic', { '--font': 'Missing Font' }), ['Segoe UI']), /not installed/)
  assert.equal(validateThemePalette(theme('classic', { '--font': 'Segoe UI' }), ['Segoe UI']).tokens['--font'], '"Segoe UI"')
})

test('background contrast includes interior colors and transparent compositing', () => {
  const background = { image: 'bg.png', fit: 'cover', dim: 0 }
  // #767676 passes 4.5 against black and white but disappears against an interior grey.
  assert.throws(() => validateThemePalette({ ...theme('classic', { '--bg': '#0000', '--text': '#767676' }), background }), /entire image range/)
  assert.doesNotThrow(() => validateThemePalette({ ...theme('wallpaper-slice', { '--bg': 'rgba(6,17,22,.85)', '--surface': '#0A1D22' }),
    background: { ...background, dim: .65 } }))
  assert.throws(() => validateThemePalette({ ...theme('wallpaper-slice', { '--text': 'rgba(212,248,239,.01)' }), background }), /contrast/)
  assert.throws(() => validateThemePalette({ ...theme(), background: { ...background, image: '../outside.png' } }), /background/)
})

test('switching and disabling releases styles and object URLs while preserving base selectors', () => {
  const styles = new Set(), created = [], revoked = []
  const document = { documentElement: { dataset: {}, style: {} }, head: { appendChild: style => styles.add(style) },
    createElement: () => { const style = { dataset: {}, textContent: '', remove: () => styles.delete(style) }; return style } }
  const renderer = createThemeApplication(document, {
    createObjectURL: () => { const value = `blob:theme-${created.length}`; created.push(value); return value },
    revokeObjectURL: value => revoked.push(value),
  })
  for (let revision = 1; revision <= 30; revision++) {
    renderer.apply({ selected: 'org.example.theme/palette', base: 'wallpaper-slice', reason: '', theme: {
      id: 'org.example.theme/palette', version: '0.1.0', revision, base: 'wallpaper-slice', tokens: { '--text': '#FFFFFF' },
      background: { image: 'bg.png', fit: 'cover', dim: .8 }, image: { mime: 'image/png', base64: 'AA==' },
    } })
    assert.equal(styles.size, 1)
    assert.equal(document.documentElement.dataset.theme, 'wallpaper-slice')
    assert.equal(document.documentElement.dataset.themeExtension, 'org.example.theme/palette')
    assert.equal(document.documentElement.style.colorScheme, 'dark')
  }
  renderer.apply({ selected: 'org.example.theme/palette', base: 'classic', reason: 'Disabled' })
  assert.equal(styles.size, 0)
  assert.deepEqual(created, revoked)
  assert.equal(document.documentElement.dataset.themeExtension, undefined)
  assert.equal(document.documentElement.dataset.theme, 'classic')
  renderer.dispose()
  assert.deepEqual(created, revoked)
})

test('installable example palettes pass the same Host rendering policy', () => {
  const file = new URL('../../examples/extensions/desktop-theme/src/amadeus_example_theme/amadeus-extension.json', import.meta.url)
  const manifest = JSON.parse(fs.readFileSync(file, 'utf8'))
  for (const contribution of manifest.contributes.theme) assert.doesNotThrow(() => validateThemePalette(contribution))
})

test('image validation bounds canvas and bytes before native decoding, and exports only the selected image', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-theme-images-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  let calls = 0
  const { imageDimensions } = loadTypeScript(new URL('../src/main/extensionImages.ts', import.meta.url), { electron: {} })
  const { ExtensionThemeResources } = loadTypeScript(new URL('../src/main/extensionThemes.ts', import.meta.url), {
    electron: { nativeImage: { createFromBuffer: () => { calls++; return { isEmpty: () => false, getSize: () => ({ width: 64, height: 64 }) } } } },
  })
  const resources = new ExtensionThemeResources()
  const manifest = { contributes: { theme: [{ ...theme(), background: { image: 'bg.png', fit: 'cover', dim: .9 } }] } }
  const original = fs.readFileSync(new URL('../../examples/extensions/desktop-theme/src/amadeus_example_theme/background.png', import.meta.url))
  fs.writeFileSync(path.join(root, 'bg.png'), original)
  assert.equal((await resources.validate(manifest, root)).palette.image, undefined)
  assert.equal((await resources.validate(manifest, root, 'palette')).palette.image.mime, 'image/png')
  const previousCalls = calls
  const oversized = Buffer.from(original); oversized.writeUInt32BE(4097, 16)
  fs.writeFileSync(path.join(root, 'bg.png'), oversized)
  await assert.rejects(resources.validate(manifest, root), /dimensions/)
  assert.equal(calls, previousCalls)
  fs.writeFileSync(path.join(root, 'bg.png'), Buffer.alloc(8 * 1024 * 1024 + 1))
  await assert.rejects(resources.validate(manifest, root), /8 MiB/)
  fs.writeFileSync(path.join(root, 'bg.png'), '<svg xmlns="http://www.w3.org/2000/svg"/>')
  await assert.rejects(resources.validate(manifest, root), /matching bytes/)
  assert.equal(calls, previousCalls)
  const jpeg = Buffer.from([255,216,255,192,0,11,8,0,20,0,30,1,1,17,0])
  assert.deepEqual(imageDimensions(jpeg, 'image/jpeg'), { width: 30, height: 20 })

})

test('theme resources refuse linked paths even when the target image is valid', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-theme-links-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.mkdirSync(path.join(root, 'package')); fs.mkdirSync(path.join(root, 'outside'))
  fs.copyFileSync(new URL('../../examples/extensions/desktop-theme/src/amadeus_example_theme/background.png', import.meta.url), path.join(root, 'outside/bg.png'))
  fs.symlinkSync(path.join(root, 'outside'), path.join(root, 'package/linked'), process.platform === 'win32' ? 'junction' : 'dir')
  const { ExtensionThemeResources } = loadTypeScript(new URL('../src/main/extensionThemes.ts', import.meta.url), { electron: {} })
  await assert.rejects(new ExtensionThemeResources().validate({ contributes: { theme: [{ ...theme(), background: { image: 'linked/bg.png', fit: 'cover', dim: .9 } }] } }, path.join(root, 'package')), /links/)
})

test('update disclosure highlights newly added contributions, commands and OS access', () => {
  const { extensionDisclosure } = loadTypeScript(new URL('../src/shared/extensions.ts', import.meta.url))
  const old = { id: 'org.example.theme', version: '0.1.0', display_name: 'Example', author: { name: 'Test' },
    runtime: { kind: 'none' }, contributes: { theme: [theme()] }, permissions: { os: { network: ['old.invalid'] } } }
  const next = structuredClone(old); next.version = '0.2.0'
  next.contributes.mcp_server = [{ id: 'notes', command: 'helper', args: ['--notes'] }]
  next.permissions.os.network.push('new.invalid')
  const added = extensionDisclosure(next, old).split('NEW OR CHANGED IN THIS UPDATE\n')[1].split('\n\n')[0]
  assert.match(added, /New contribution: mcp_server\/notes/)
  assert.match(added, /helper --notes/)
  assert.match(added, /new.invalid/)
  assert.ok(!added.includes('old.invalid'))
})


test('base identity and opaque surfaces bound nested palette composition', () => {
  const dark = { '--bg': '#101010', '--surface': '#121212', '--surface-alt': '#151515', '--text': '#fff', '--muted': '#ccc', '--faint': '#bbb', '--accent': '#707070', ...Object.fromEntries(['--hover','--pressed','--selection-bg','--subtle-fill','--selected-fill','--focus-fill'].map(name => [name, '#181818'])) }
  assert.throws(() => validateThemePalette(theme('classic', dark)), /base brightness/)
  assert.throws(() => validateThemePalette(theme('wallpaper-slice', { '--surface': 'rgba(10,29,34,.8)' })), /opaque/)
  assert.throws(() => validateThemePalette(theme('wallpaper-slice', { '--surface-alt': 'rgba(13,37,42,.5)' })), /opaque/)
})

test('direct status text, accent links and faint text cannot disappear on package surfaces', () => {
  assert.throws(() => validateThemePalette(theme('classic', { '--surface': '#bde7a3' })), /danger|success|contrast/)
  assert.throws(() => validateThemePalette(theme('wallpaper-slice', { '--accent': '#0d252a' })), /accent.*contrast/)
  assert.throws(() => validateThemePalette(theme('classic', { '--faint': '#ffffff' })), /faint.*contrast/)
  assert.throws(() => validateThemePalette(theme('classic', { '--on-accent': '#fff' })), /not an allowed/)
})


test('accent text on either base receives a readable Host-owned button label', () => {
  const light = validateThemePalette(theme('classic'))
  const dark = validateThemePalette(theme('wallpaper-slice'))
  assert.equal(light.tokens['--on-accent'], '#ffffff')
  assert.equal(dark.tokens['--on-accent'], '#000000')
  // Independently evaluate the opaque accent fixtures, including hover.
  const luminance = color => parseThemeColor(color).slice(0, 3).reduce((sum, value, i) =>
    sum + (value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4) * [.2126,.7152,.0722][i], 0)
  for (const palette of [light, dark]) for (const key of ['--accent', '--accent-hover']) {
    const fill = palette.tokens[key] || (key === '--accent' ? '#111827' : '#1F2937')
    const a = luminance(fill), b = luminance(palette.tokens['--on-accent'])
    assert.ok((Math.max(a, b) + .05) / (Math.min(a, b) + .05) >= 4.5)
  }
})


test('pinned protocol color cases match the Host parser', () => {
  const cases = JSON.parse(fs.readFileSync(new URL('../../tests/fixtures/extension_theme_colors.json', import.meta.url), 'utf8'))
  for (const value of cases.accepted) assert.doesNotThrow(() => parseThemeColor(value), value)
  for (const value of cases.rejected) assert.throws(() => parseThemeColor(value), undefined, value)
})

test('the pinned canonical author example passes Host palette validation', () => {
  const manifest = JSON.parse(fs.readFileSync(new URL('../../tests/fixtures/extension_protocol_theme.json', import.meta.url), 'utf8'))
  for (const theme of manifest.contributes.theme) assert.doesNotThrow(() => validateThemePalette(theme))
})

test('v0 rejects WebP declarations before image decoding', () => {
  assert.throws(() => validateThemePalette({ ...theme(), background: { image: 'bg.webp', fit: 'cover', dim: .8 } }), /background/)
})
