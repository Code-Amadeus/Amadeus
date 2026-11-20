import { themeBases, themeTokenNames } from './themeTokens.generated.js'
import type { ExtensionContribution } from './extensions.js'

export type BuiltinTheme = 'classic' | 'wallpaper-slice'
type Color = [number, number, number, number]
export type ThemePalette = { base: BuiltinTheme; tokens: Record<string, string>; background?: { image: string; fit: 'cover' | 'contain'; dim: number } }
export type AppliedTheme = ThemePalette & { id: string; version: string; revision: number; image?: { mime: string; base64: string } }
export type DesktopThemeState = { selected: string; base: BuiltinTheme; reason: string; theme?: AppliedTheme }

function fail(field: string, detail: string): never { throw new Error(`Theme ${field}: ${detail}`) }
function number(value: string, maximum: number, unit?: string): number {
  const match = /^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(%|deg)?$/.exec(value)
  if (!match || unit && match[2] !== unit || match[2] === 'deg' && unit !== 'deg') throw new Error('Invalid color component')
  const valueNumber = Number(match[1])
  const result = match[2] === '%' ? valueNumber * maximum / 100 : valueNumber
  if (!Number.isFinite(result) || result < 0 || result > maximum) throw new Error('Color component is outside its range')
  return result
}

export function parseThemeColor(value: string): Color {
  const text = value.trim().toLowerCase()
  if (/^#(?:[a-f0-9]{3,4}|[a-f0-9]{6}|[a-f0-9]{8})$/.test(text)) {
    let digits = text.slice(1)
    if (digits.length < 5) digits = [...digits].map(x => x + x).join('')
    return [0, 2, 4].map(offset => parseInt(digits.slice(offset, offset + 2), 16) / 255)
      .concat(digits.length === 8 ? parseInt(digits.slice(6), 16) / 255 : 1) as Color
  }
  const match = /^(rgb|rgba|hsl|hsla)\(([^()]*)\)$/.exec(text)
  if (!match) throw new Error('Use a hex, rgb/rgba or hsl/hsla literal')
  const body = match[2].trim()
  let parts: string[]
  if (body.includes(',')) {
    if (body.includes('/')) throw new Error('Do not mix comma and slash color syntax')
    parts = body.split(',').map(part => part.trim())
    if (match[1].startsWith('rgb') && new Set(parts.slice(0, 3).map(part => part.endsWith('%'))).size !== 1) throw new Error('Comma RGB components must use consistent units')
  } else {
    const groups = body.split('/')
    if (groups.length > 2) throw new Error('Only one alpha separator is allowed')
    parts = groups[0].trim().split(/\s+/)
    if (parts.length !== 3) throw new Error('Space color syntax requires three components')
    if (groups.length === 2) parts.push(groups[1].trim())
  }
  if (parts.length < 3 || parts.length > 4) throw new Error('Invalid color component count')
  const alpha = parts.length === 4 ? number(parts[3], 1) : 1
  if (match[1].startsWith('rgb')) return [...parts.slice(0, 3).map(x => number(x, 255) / 255), alpha] as Color
  const hue = /^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(deg)?$/.exec(parts[0])
  if (!hue || !Number.isFinite(Number(hue[1]))) throw new Error('Invalid hue')
  const h = (Number(hue[1]) % 360 + 360) % 360 / 30
  const saturation = number(parts[1], 1, '%'), light = number(parts[2], 1, '%')
  const amplitude = saturation * Math.min(light, 1 - light)
  return [0, 8, 4].map(n => light - amplitude * Math.max(-1, Math.min((n + h) % 12 - 3, 9 - (n + h) % 12, 1)))
    .concat(alpha) as Color
}
function over(foreground: Color, background: Color): Color {
  return [0, 1, 2].map(i => foreground[i] * foreground[3] + background[i] * (1 - foreground[3])).concat(1) as Color
}
function luminance(color: Color): number {
  return color.slice(0, 3).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
    .reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0)
}
function contrast(a: number, b: number): number { return (Math.max(a, b) + .05) / (Math.min(a, b) + .05) }

/** Conservative interval proof, including interior minima and transparent text.
 * All composite channels increase with the unknown image's independent RGB channels.
 * Independent luminance intervals give a lower bound; subdivision tightens it.
 */
function readable(foreground: Color, surface: (image: Color) => Color, minimum: number): boolean {
  const visit = (left: Color, right: Color, depth: number): boolean => {
    const a = surface(left), b = surface(right)
    const bg = [luminance(a), luminance(b)], fg = [luminance(over(foreground, a)), luminance(over(foreground, b))]
    if (contrast(bg[0], fg[0]) + 1e-9 < minimum || contrast(bg[1], fg[1]) + 1e-9 < minimum) return false
    const lower = fg[0] >= bg[1] ? contrast(fg[0], bg[1]) : bg[0] >= fg[1] ? contrast(bg[0], fg[1]) : 1
    if (lower + 1e-9 >= minimum) return true
    if (depth === 18) return false
    const channel = depth % 3, mid = (left[channel] + right[channel]) / 2
    const low = [...right] as Color, high = [...left] as Color
    low[channel] = high[channel] = mid
    return visit(left, low, depth + 1) && visit(high, right, depth + 1)
  }
  return visit([0, 0, 0, 1], [1, 1, 1, 1], 0)
}

export function validateThemePalette(theme: ExtensionContribution, installedFonts: readonly string[] = []): ThemePalette {
  if (theme.base !== 'classic' && theme.base !== 'wallpaper-slice') fail('base', 'unsupported base theme')
  if (!theme.tokens || typeof theme.tokens !== 'object' || Array.isArray(theme.tokens)) fail('tokens', 'an object is required')
  const base = theme.base, source = { ...themeBases[base] }
  const tokens: Record<string, string> = {}
  for (const [key, raw] of Object.entries(theme.tokens)) {
    if (!(themeTokenNames as readonly string[]).includes(key)) fail(`tokens.${key}`, 'not an allowed theme token')
    if (typeof raw !== 'string' || !raw || raw.length > 256 || /url\s*\(|var\s*\(|[;{}\\]|\/\*|\*\//i.test(raw)) fail(`tokens.${key}`, 'use a literal value')
    if (key === '--font' || key === '--font-cjk') {
      if (!/^[\p{L}\p{N} '"-]{1,128}$/u.test(raw)) fail(`tokens.${key}`, 'use one installed font family')
      const family = raw.trim().replace(/^(['"])(.*?)\1$/, '$2')
      if (family.includes('"') || family.includes("'") || !installedFonts.some(name => name.toLowerCase() === family.toLowerCase())) fail(`tokens.${key}`, 'font family is not installed')
      tokens[key] = JSON.stringify(family)
    } else {
      let color: Color
      try { color = parseThemeColor(raw) } catch (error) { fail(`tokens.${key}`, (error as Error).message) }
      tokens[key] = `rgba(${color![0] * 255}, ${color![1] * 255}, ${color![2] * 255}, ${color![3]})`
    }
    source[key] = tokens[key]
  }
  const colors = Object.fromEntries(Object.entries(source).filter(([key]) => !key.startsWith('--font')).map(([key, value]) => [key, parseThemeColor(value)]))
  const background = theme.background
  if (background && (typeof background.image !== 'string' || !/\.(?:png|jpe?g)$/i.test(background.image) || !/^[A-Za-z0-9_][A-Za-z0-9._-]*(\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/.test(background.image)
    || !['cover', 'contain'].includes(background.fit) || !Number.isFinite(background.dim) || background.dim < 0 || background.dim > 1)) fail('background', 'invalid image, fit or dim')
  const baseBackground = parseThemeColor(themeBases[base]['--bg'])
  if (!background) {
    // The renderer uses this opaque canvas, not repeated alpha over base gradients.
    colors['--bg'] = over(colors['--bg'], baseBackground)
    const color = colors['--bg']
    tokens['--bg'] = `rgba(${color[0] * 255}, ${color[1] * 255}, ${color[2] * 255}, 1)`
  }
  const canvas = colors['--bg']
  const first = (image: Color): Color => over(canvas, background ? [image[0] * (1 - background.dim), image[1] * (1 - background.dim), image[2] * (1 - background.dim), 1] : baseBackground)
  const low = first([0, 0, 0, 1]), high = first([1, 1, 1, 1])
  // Some pages paint --bg again. All repetitions stay between the first
  // composite and opaque canvas; prove contrast over that complete interval.
  const bg = (image: Color): Color => [0, 1, 2].map(i => {
    const minimum = Math.min(low[i], canvas[i]), maximum = Math.max(high[i], canvas[i])
    return minimum + (maximum - minimum) * image[i]
  }).concat(1) as Color
  for (const key of ['--surface', '--surface-alt']) if (colors[key][3] !== 1) fail(`tokens.${key}`, 'surface colors must be opaque')
  const surfaces: Array<[string, (image: Color) => Color]> = [['--bg', bg]]
  for (const key of ['--surface', '--surface-alt']) surfaces.push([key, image => over(colors[key], bg(image))])
  const baseSurfaces = [...surfaces]
  for (const [name, surface] of baseSurfaces) {
    const dark = luminance(surface([0, 0, 0, 1])), light = luminance(surface([1, 1, 1, 1]))
    if (base === 'classic' ? dark < .5 : light > .1) fail(`tokens.${name}`, 'base brightness must stay light for classic and dark for wallpaper-slice across the entire image range')
  }
  for (const [name, surface] of baseSurfaces) for (const key of ['--hover', '--pressed', '--selection-bg', '--subtle-fill', '--selected-fill', '--focus-fill']) {
    surfaces.push([`${name}/${key}`, image => over(colors[key], surface(image))])
  }
  for (const [name, surface] of surfaces) for (const [key, minimum] of [['--text', 4.5], ['--muted', 3], ['--faint', 3], ['--accent', 4.5]] as const) {
    if (!readable(colors[key], surface, minimum)) fail(`tokens.${key}`, `contrast must be at least ${minimum} on ${name}, including the entire image range`)
  }
  for (const [name, surface] of surfaces) {
    for (const state of ['danger', 'warning', 'success']) {
      if (!readable(colors[`--${state}`], surface, 4.5)) fail(`tokens.${name}`, `Host ${state} text requires contrast of at least 4.5`)
    }
    for (const state of ['danger', 'warning', 'success', 'neutral-pill']) {
      if (!readable(colors[`--${state}`], image => over(colors[`--${state}-bg`], surface(image)), state === 'neutral-pill' ? 3 : 4.5)) fail(`tokens.${name}`, `Host ${state} label must remain readable`)
    }
  }
  // Accent is both text and a fill. Fixed white labels are incompatible with
  // readable accent text on dark surfaces; the Host owns the label color.
  const label = [[1, 1, 1, 1], [0, 0, 0, 1]].find(foreground =>
    ['--accent', '--accent-hover'].every(key => surfaces.every(([, surface]) => readable(foreground as Color, image => over(colors[key], surface(image)), 4.5))))
  if (!label) fail('tokens.--accent', 'accent and hover fills need one readable black or white label')
  tokens['--on-accent'] = label[0] ? '#ffffff' : '#000000'
  return { base, tokens, ...(background ? { background } : {}) }
}
