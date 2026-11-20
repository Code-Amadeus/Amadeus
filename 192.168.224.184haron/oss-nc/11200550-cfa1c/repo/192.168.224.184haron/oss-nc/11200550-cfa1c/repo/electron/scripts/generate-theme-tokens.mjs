import assert from 'node:assert/strict'
import fs from 'node:fs'

const css = fs.readFileSync(new URL('../src/renderer/styles/index.css', import.meta.url), 'utf8')
const bases = {}
for (const match of css.matchAll(/^:root(?:\[data-theme='([^']+)'\])? \{([^}]+)\}/gm)) {
  bases[match[1] || 'classic'] = Object.fromEntries([...match[2].matchAll(/(--[a-z-]+):\s*([^;]+);/g)].map(row => [row[1], row[2].trim()]))
}
assert.ok(bases.classic && bases['wallpaper-slice'])
bases['wallpaper-slice'] = { ...bases.classic, ...bases['wallpaper-slice'] }
const contract = JSON.parse(fs.readFileSync(new URL('../../server/extensions/theme-tokens.v0.json', import.meta.url), 'utf8'))
const tokens = Object.keys(contract)
assert.ok(tokens.every(name => Object.hasOwn(bases.classic, name)), 'Published theme tokens need Host defaults')
const content = `// Generated from renderer/styles/index.css and pinned protocol theme-tokens.v0.json; do not edit.\nexport const themeBases: Record<string, Record<string, string>> = ${JSON.stringify(bases, null, 2)}\nexport const themeTokenNames = ${JSON.stringify(tokens)} as const\n`
const target = new URL('../src/shared/themeTokens.generated.ts', import.meta.url)
if (process.argv.includes('--check')) assert.equal(fs.readFileSync(target, 'utf8'), content, 'Theme token profile is out of date')
else fs.writeFileSync(target, content)
