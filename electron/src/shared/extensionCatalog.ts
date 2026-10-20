export type CatalogEntry = {
  id: string; version: string; sha256: string; url: string; size: number
  display: Record<string, unknown>; compatibility: Record<string, unknown>
  publisher: Record<string, unknown>; review: Record<string, unknown>
  withdrawal: null | { reason: 'maintainer' | 'copyright'; evidence: string }
  incompatible: string[]
}
export type CatalogIndex = {
  format: 'amadeus.catalog/v1'; sequence: number; entries: CatalogEntry[]
  security: Array<{ id: string; version: string; sha256: string; status: 'blocked' | 'allowed'; sequence: number; reason: string; evidence: string }>
}
export type CatalogRequest = { action: 'get' | 'refresh' | 'recover' | 'cancel' }
  | { action: 'install'; id: string; version: string }
  | { action: 'homepage'; id: string; version: string }
export type CatalogView = { installed?: Array<{ id: string; version: string; digest: string; catalogState: 'matched' | 'unlisted' | 'unavailable'; review?: Record<string, unknown>; withdrawal?: CatalogEntry['withdrawal']; blocked: boolean }>; entries: CatalogEntry[]; sequence?: number; checkedAt?: string; cached: boolean; error?: string; downloading?: { id: string; version: string; received: number; total: number } }
export type CatalogReply = { ok: boolean; error?: string; cancelled?: boolean; catalog?: CatalogView; settings?: Record<string, unknown> }
