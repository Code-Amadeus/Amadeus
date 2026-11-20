import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/renderer/components/providerPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('exports', compiled)(exports)
const select = exports.preserveOrChooseProvider

test('new Work follows the Host-selected provider, including custom registered agents', () => {
  assert.equal(select('', ['codex', 'pi', 'openclaw'], 'pi'), 'pi')
  assert.equal(select('', ['codex', 'custom-agent'], 'custom-agent'), 'custom-agent')
})

test('an explicit selection is preserved while a missing default never selects another agent', () => {
  assert.equal(select('openclaw', ['codex', 'pi', 'openclaw'], 'pi'), 'openclaw')
  assert.equal(select('', ['codex', 'openclaw'], 'pi'), '')
  assert.equal(select('removed', ['codex'], 'pi'), '')
})
