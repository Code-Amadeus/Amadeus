import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Console scope. The desktop projection surfaces (Work window, CRT work surface, workboard, wallpaper
// projection, Work preview) keep their own art direction in workSurface.css, workPreview.css and the Work
// components; everything else draws from the role tokens in tokens.css and the theme palette.
const renderer = fileURLToPath(new URL('../src/renderer', import.meta.url))
const consoleStylesheets = ['index.css', 'characterVisuals.css', 'characterWorkspace.css']
const projectionComponent = file => /[\\/]components[\\/]work[\\/]/.test(file) || /Work(Preview)?Page\.tsx$/.test(file)

function consoleComponents() {
  const files = []
  const walk = directory => {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, item.name)
      if (item.isDirectory()) walk(filename)
      else if (filename.endsWith('.tsx') && !projectionComponent(filename)) files.push(filename)
    }
  }
  walk(renderer)
  return files
}

/** Every declaration with the chain of selectors and at-rule preludes that contains it. */
function declarations(file) {
  const css = fs.readFileSync(path.join(renderer, 'styles', file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
  const found = []
  const context = []
  let start = 0
  for (let index = 0; index < css.length; index += 1) {
    const char = css[index]
    if (char === '{') {
      context.push(css.slice(start, index).trim().replace(/\s+/g, ' '))
      start = index + 1
    } else if (char === ';' || char === '}') {
      const chunk = css.slice(start, index).trim()
      const colon = chunk.indexOf(':')
      if (context.length && colon > 0) {
        found.push({ file, context: [...context], property: chunk.slice(0, colon).trim(), value: chunk.slice(colon + 1).trim() })
      }
      if (char === '}') context.pop()
      start = index + 1
    }
  }
  return found
}

const consoleDeclarations = consoleStylesheets.flatMap(declarations)
// Theme blocks define the palette itself; custom properties name local palettes (log viewer, preview stage).
const definesPalette = declaration => declaration.property.startsWith('--')
  || declaration.context.some(prelude => prelude.startsWith(':root') || prelude.startsWith("html[data-theme='"))
const where = declaration => `${declaration.file} ${declaration.context.join(' › ')} { ${declaration.property}: ${declaration.value} }`
const colorLiteral = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i

test('console stylesheets size text, weight and corners with the role tokens', () => {
  const role = {
    'font-size': value => value === 'inherit' || /^var\(--font-(caption|small|body|subtitle|title|page)\)$/.test(value),
    'font-weight': value => /^var\(--weight-(regular|medium|strong)\)$/.test(value),
    'border-radius': value => value.split(/\s+/).every(part => part === '0' || /^var\(--radius-(chip|control|container|pill)\)$/.test(part)),
  }
  const offScale = consoleDeclarations.filter(declaration => role[declaration.property] && !role[declaration.property](declaration.value))
  assert.deepEqual(offScale.map(where), [], 'Use the tokens in tokens.css for font-size, font-weight and border-radius')
})

test('the console type scale has six sizes and nothing below 11px', () => {
  const sizes = declarations('tokens.css').filter(item => item.property.startsWith('--font-')).map(item => Number.parseFloat(item.value))
  assert.equal(new Set(sizes).size, 6)
  assert.ok(Math.min(...sizes) >= 11, `smallest console text is ${Math.min(...sizes)}px`)
})

test('console colors come from the theme palette', () => {
  const literals = consoleDeclarations.filter(declaration => !definesPalette(declaration) && colorLiteral.test(declaration.value))
  assert.deepEqual(literals.map(where), [], 'Use theme tokens (var(--…)) instead of color literals')
})

test('console components keep visual styling in CSS classes', () => {
  // Visual properties belong in classes; inline style is reserved for runtime-computed layout (widths, offsets).
  const visual = new Set(['color', 'background', 'backgroundColor', 'border', 'borderTop', 'borderRight', 'borderBottom', 'borderLeft',
    'borderColor', 'borderRadius', 'boxShadow', 'fontSize', 'fontWeight', 'fontFamily', 'lineHeight', 'letterSpacing', 'opacity', 'outline', 'textTransform'])
  const findings = { inlineVisualStyle: [], arbitraryClass: [], colorLiteral: [], scriptedHover: [] }
  for (const filename of consoleComponents()) {
    const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    const at = node => `${path.relative(renderer, filename)}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}`
    const texts = node => {
      const values = []
      const collect = child => {
        if (ts.isStringLiteralLike(child) || ts.isTemplateHead(child) || ts.isTemplateMiddle(child) || ts.isTemplateTail(child)) values.push(child.text)
        ts.forEachChild(child, collect)
      }
      collect(node)
      return values
    }
    const visit = node => {
      if (ts.isJsxAttribute(node) && node.initializer) {
        const name = node.name.getText(source)
        if (name === 'style') {
          const keys = []
          const collect = child => {
            if (ts.isPropertyAssignment(child) || ts.isShorthandPropertyAssignment(child)) keys.push(child.name.getText(source))
            ts.forEachChild(child, collect)
          }
          collect(node.initializer)
          for (const key of keys.filter(key => visual.has(key))) findings.inlineVisualStyle.push(`${at(node)} ${key}`)
        }
        if (name === 'className') {
          for (const token of texts(node.initializer).flatMap(value => value.split(/\s+/))) {
            if (/\[[^\]]+\]/.test(token)) findings.arbitraryClass.push(`${at(node)} ${token}`)
          }
        }
        if (/^onMouse(Enter|Leave|Over|Out)$/.test(name) && /\.style\./.test(node.initializer.getText(source))) {
          findings.scriptedHover.push(`${at(node)} ${name}`)
        }
      }
      if (ts.isStringLiteralLike(node) && colorLiteral.test(node.text)) findings.colorLiteral.push(`${at(node)} ${JSON.stringify(node.text)}`)
      ts.forEachChild(node, visit)
    }
    visit(source)
  }
  assert.deepEqual(findings, { inlineVisualStyle: [], arbitraryClass: [], colorLiteral: [], scriptedHover: [] },
    'Move visual styles into CSS classes on the role tokens; use :hover in CSS')
})
