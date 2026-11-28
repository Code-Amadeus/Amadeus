export type MemoryLookupStatus = 'searching' | 'found' | 'empty' | 'partial' | 'unavailable' | 'invalidated'

export interface MemoryLookupReceipt {
  sessionId: string
  turnId: string
  status: MemoryLookupStatus
}

export interface MemoryLookupRequest {
  sessionId: string
  turnId: string
  pending: boolean
}

export const memoryLookupStatusText: Record<MemoryLookupStatus, string> = {
  searching: 'Searching saved memories…',
  found: 'Saved memory records found.',
  empty: 'No matching records in the available memory index.',
  partial: 'Memory index coverage is incomplete; results may be missing.',
  unavailable: 'Saved memory search is temporarily unavailable.',
  invalidated: 'This memory search is no longer current.',
}

// Only host receipts for this explicit request can describe its execution.
// A terminal receipt cannot be replaced by a delayed searching event. Revocation
// is authoritative even when it arrives after a result.
export function acceptMemoryLookupReceipt(
  request: MemoryLookupRequest | null,
  previous: MemoryLookupReceipt | null,
  payload: Record<string, unknown>,
  activeSessionId: string,
  interrupted: Set<string>,
  switching: boolean,
): MemoryLookupReceipt | null {
  if (!request || switching || !activeSessionId || request.sessionId !== activeSessionId
    || payload.session_id !== request.sessionId || payload.turn_id !== request.turnId
    || interrupted.has(request.turnId)) return previous
  const status = payload.status
  if (typeof status !== 'string' || !Object.hasOwn(memoryLookupStatusText, status)) return previous
  if (previous?.status === status) return previous
  if (previous && previous.status !== 'searching' && status !== 'invalidated') return previous
  return { sessionId: request.sessionId, turnId: request.turnId, status: status as MemoryLookupStatus }
}
