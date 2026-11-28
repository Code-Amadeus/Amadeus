/**
 * Durable store for the full-display Slice surface's draggable Work controls.
 *
 * The renderer used to keep these positions in localStorage, but the Slice
 * window loads from `http://127.0.0.1:<assetPort>` and the AssetServer picks a
 * free port each run, so the origin — and with it the saved positions — resets
 * every restart. The main process owns durable state; the renderer only sees
 * normalized window coordinates, never the file.
 */

import fs from 'node:fs'
import path from 'node:path'

export type SliceControlPosition = { x: number; y: number }
export type SliceControlPositions = Partial<Record<'status' | 'card', SliceControlPosition>>

const CONTROL_KEYS = ['status', 'card'] as const

export function normalizeSliceControlPositions(raw: unknown): SliceControlPositions {
  const positions: SliceControlPositions = {}
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return positions
  const record = raw as Record<string, unknown>
  for (const key of CONTROL_KEYS) {
    const pos = record[key]
    if (!pos || typeof pos !== 'object' || Array.isArray(pos)) continue
    const x = Number((pos as Record<string, unknown>).x)
    const y = Number((pos as Record<string, unknown>).y)
    // Positions are window-normalized; the renderer clamps to [-1, 1] so a
    // control may live partially past the left edge. Reject anything wider.
    if (Number.isFinite(x) && Number.isFinite(y) && x >= -1 && x <= 1 && y >= -1 && y <= 1) {
      positions[key] = { x, y }
    }
  }
  return positions
}

export class SliceControlPositionsStore {
  private readonly filePath: string

  constructor(filePath: string) {
    this.filePath = filePath
  }

  read(): SliceControlPositions {
    try {
      return normalizeSliceControlPositions(JSON.parse(fs.readFileSync(this.filePath, 'utf8')))
    } catch {
      // Missing or corrupt storage starts clean, same as the old localStorage path.
      return {}
    }
  }

  write(positions: SliceControlPositions): void {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true })
    const temporary = `${this.filePath}.tmp`
    const handle = fs.openSync(temporary, 'w', 0o600)
    try {
      fs.writeFileSync(handle, `${JSON.stringify(positions, null, 2)}\n`, 'utf8')
      fs.fsyncSync(handle)
    } finally {
      fs.closeSync(handle)
    }
    fs.renameSync(temporary, this.filePath)
  }
}
