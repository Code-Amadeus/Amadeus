import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { normalizeSliceControlPositions, SliceControlPositionsStore } from '../src/main/sliceControlPositions.ts'

test('normalize keeps finite in-range status and card positions only', () => {
  assert.deepEqual(normalizeSliceControlPositions({
    status: { x: 0.5, y: 0.9 },
    card: { x: -0.5, y: 0.1 },
    other: { x: 0.1, y: 0.1 },                 // unknown control key dropped
    broken: 'not-an-object',
    outlier: { x: 3, y: 0 },                   // outside [-1, 1]
    nan: { x: Number.NaN, y: 0 },
  }), {
    status: { x: 0.5, y: 0.9 },
    card: { x: -0.5, y: 0.1 },
  })
  assert.deepEqual(normalizeSliceControlPositions(null), {})
  assert.deepEqual(normalizeSliceControlPositions([{ x: 1, y: 1 }]), {})
  assert.deepEqual(normalizeSliceControlPositions('{"status":{"x":1,"y":1}}'), {})
})

test('the store round-trips positions and starts clean on missing or corrupt data', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-slice-positions-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const file = path.join(dir, 'nested', 'positions.json')
  const store = new SliceControlPositionsStore(file)

  assert.deepEqual(store.read(), {}, 'a missing file reads as no positions')
  store.write({ status: { x: 0.25, y: 0.75 } })
  assert.deepEqual(store.read(), { status: { x: 0.25, y: 0.75 } })
  assert.equal((fs.statSync(file).mode & 0o777) & 0o077, 0, 'positions are not world-readable')

  fs.writeFileSync(file, 'not-json', 'utf8')
  assert.deepEqual(store.read(), {}, 'corrupt storage reads as no positions')

  store.write({ card: { x: -0.2, y: 0.4 } })
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { card: { x: -0.2, y: 0.4 } })
})
