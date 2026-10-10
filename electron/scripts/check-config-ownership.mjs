import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const root = new URL('../../', import.meta.url)
const baseline = JSON.parse(fs.readFileSync(new URL('config/catalog_legacy.json', root), 'utf8'))
const declarations = fs.readdirSync(new URL('config/catalog/', root), { recursive: true }).filter(name => name.endsWith('.json'))
const declared = new Set(declarations.flatMap(name => Object.keys(JSON.parse(fs.readFileSync(new URL(`config/catalog/${name.replaceAll('\\', '/')}`, root), 'utf8')).config)))
export function handwrittenKeys(file, source) {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
  const keys = new Set()
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ['VALUE_KEYS', 'SECRET_KEYS'].includes(node.name.getText(tree))) {
      const collect = child => {
        if (ts.isStringLiteral(child) && /^[A-Z][A-Z0-9_]*$/.test(child.text)) keys.add(child.text)
        ts.forEachChild(child, collect)
      }
      if (node.initializer) collect(node.initializer)
    }
    if (ts.isCallExpression(node) && node.expression.getText(tree) === 'field' && ts.isStringLiteral(node.arguments[0])) keys.add(node.arguments[0].text)
    ts.forEachChild(node, visit)
  }
  visit(tree)
  return [...keys].sort()
}
for (const [file, allowed] of Object.entries(baseline.typescript)) {
  const found = handwrittenKeys(file, fs.readFileSync(new URL(file, root), 'utf8'))
  assert.deepEqual(found.filter(key => !allowed.includes(key) || declared.has(key)), [], `${file}: new or migrated handwritten declarations; edit the JSON catalog`)
}
