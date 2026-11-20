import { createHash } from 'node:crypto'

export type ArtifactIdentity = { id: string; version: string; digest: string }
export type SecurityDecision = ArtifactIdentity & { blocked: boolean; sequence: number }
export type ExtensionSecurity = { status: 'ready'; sequence: number; decisions: SecurityDecision[]; checkedAt: string; checksum: string }
  | { status: 'unavailable'; floor: number | null }
export const securityKey = (item: ArtifactIdentity): string => JSON.stringify([item.id, item.version, item.digest])
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const ordered = (decisions: SecurityDecision[]) => [...decisions].sort((a, b) => securityKey(a) < securityKey(b) ? -1 : securityKey(a) > securityKey(b) ? 1 : 0)
function checksum(sequence: number, decisions: SecurityDecision[], checkedAt: string): string {
  return createHash('sha256').update(JSON.stringify([sequence, decisions.map(d => [d.id, d.version, d.digest, d.blocked, d.sequence]), checkedAt])).digest('hex')
}
export function securitySnapshot(sequence: number, decisions: SecurityDecision[], checkedAt: string): ExtensionSecurity {
  decisions = ordered(decisions)
  return { status: 'ready', sequence, decisions, checkedAt, checksum: checksum(sequence, decisions, checkedAt) }
}
export function parseSecurity(value: unknown): ExtensionSecurity | undefined {
  if (value === undefined) return undefined // valid pre-catalog settings
  const bad: ExtensionSecurity = { status: 'unavailable', floor: null }
  if (!value || typeof value !== 'object') return bad
  const v = value as ExtensionSecurity
  if (v.status === 'unavailable') return { status: 'unavailable', floor: integer(v.floor) ? v.floor : null }
  if (v.status !== 'ready' || !integer(v.sequence) || !Array.isArray(v.decisions) || v.decisions.length > 16384
    || typeof v.checkedAt !== 'string' || !Number.isFinite(Date.parse(v.checkedAt))) return bad
  const keys = new Set<string>()
  for (const d of v.decisions) {
    if (!d || typeof d.id !== 'string' || !d.id || d.id.length > 128 || typeof d.version !== 'string'
      || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(d.version)
      || typeof d.digest !== 'string' || !/^[a-f0-9]{64}$/.test(d.digest) || typeof d.blocked !== 'boolean'
      || !integer(d.sequence) || d.sequence < 1 || d.sequence > v.sequence || keys.has(securityKey(d))) return bad
    keys.add(securityKey(d))
  }
  if (v.checksum !== checksum(v.sequence, v.decisions, v.checkedAt)) return bad
  return structuredClone(v)
}
function sameDecisions(a: Extract<ExtensionSecurity, { status: 'ready' }>, b: Extract<ExtensionSecurity, { status: 'ready' }>): boolean {
  return JSON.stringify(ordered(a.decisions).map(d => [d.id, d.version, d.digest, d.blocked, d.sequence]))
    === JSON.stringify(ordered(b.decisions).map(d => [d.id, d.version, d.digest, d.blocked, d.sequence]))
}
/** Read both complete copies. A persisted fault cannot be cleared by an ordinary save. */
export function recoverSecurity(copies: Array<ExtensionSecurity | undefined>, corrupt: boolean): ExtensionSecurity | undefined {
  const ready = copies.filter((s): s is Extract<ExtensionSecurity, { status: 'ready' }> => s?.status === 'ready').sort((a, b) => b.sequence - a.sequence)
  const faults = copies.filter((s): s is Extract<ExtensionSecurity, { status: 'unavailable' }> => s?.status === 'unavailable')
  const best = ready[0]
  if (best && ready.some(s => s.sequence === best.sequence && !sameDecisions(s, best))) return { status: 'unavailable', floor: best.sequence }
  // A malformed field is treated like damaged bytes: an intact copy can recover it.
  // Persisted sentinels are distinguished by the read owner and supplied here as faults.
  if (faults.length) {
    const floor = faults.some(s => s.floor === null) ? null : Math.max(...faults.map(s => s.floor!))
    if (!best || floor === null || best.sequence <= floor) return { status: 'unavailable', floor }
  }
  if (best) return best
  return corrupt ? { status: 'unavailable', floor: null } : undefined
}
export function acceptSecurity(previous: ExtensionSecurity | undefined, next: ExtensionSecurity, recovery: boolean): ExtensionSecurity {
  if (next.status !== 'ready' || parseSecurity(next)?.status !== 'ready') throw new Error('Invalid extension security snapshot')
  if (previous?.status === 'unavailable') {
    if (!recovery) throw new Error('Extension security requires explicit official recovery')
    if (previous.floor !== null && next.sequence < previous.floor) throw new Error('Catalog sequence rollback rejected')
  } else if (previous) {
    if (next.sequence < previous.sequence) throw new Error('Catalog sequence rollback rejected')
    if (next.sequence === previous.sequence && !sameDecisions(previous, next)) throw new Error('Conflicting catalog security at the same sequence')
    const incoming = new Map(next.decisions.map(d => [securityKey(d), d]))
    const known = new Set(previous.decisions.map(securityKey))
    for (const old of previous.decisions) {
      const current = incoming.get(securityKey(old))
      if (!current || current.sequence < old.sequence || (current.sequence === old.sequence && current.blocked !== old.blocked)
        || (current.blocked !== old.blocked && current.sequence <= previous.sequence)) throw new Error('Known security decisions cannot disappear or regress')
    }
    for (const current of next.decisions) {
      if (!known.has(securityKey(current)) && current.sequence <= previous.sequence) throw new Error('New security decision predates accepted catalog')
    }
  }
  return next
}
export function securityBlock(state: ExtensionSecurity | undefined, identity?: ArtifactIdentity): string {
  if (state?.status === 'unavailable') return 'Extension security state is damaged. Recover from the official catalog before installing or activating extensions.'
  if (identity && state?.status === 'ready' && state.decisions.some(d => securityKey(d) === securityKey(identity) && d.blocked)) return 'This exact extension artifact is blocked for security reasons.'
  return ''
}
