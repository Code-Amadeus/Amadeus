import { createHash } from 'node:crypto'
import path from 'node:path'

/** App data follows the Work's entry, not an Attempt, Attach ticket or window. */
export function auipStoragePartition(workItemId: string, entryPath: string): string {
  let entry = path.resolve(entryPath)
  if (process.platform === 'win32') entry = entry.toLowerCase()
  const identity = JSON.stringify([workItemId, entry])
  return `persist:auip-work-${createHash('sha256').update(identity).digest('hex')}`
}
