// Persistent descendant cleanup proof currently exists only on Windows.
// Other platforms retain version-isolated installation and restart-to-apply.
export function usesScheduledExtensionUpdates(platform: string | undefined): boolean {
  return platform === 'win32'
}

export type CatalogEntry = {
  id: string; version: string; sha256: string; url: string; size: number
  display: Record<string, unknown>; compatibility: Record<string, unknown>
  publisher: Record<string, unknown>; review: Record<string, unknown>
  withdrawal: null | { reason: 'maintainer' | 'copyright'; evidence: string }
  incompatible: string[]; blockedReason?: string
}
export type CatalogIndex = {
  format: 'amadeus.catalog/v1'; sequence: number; entries: CatalogEntry[]
  security: Array<{ id: string; version: string; sha256: string; status: 'blocked' | 'allowed'; sequence: number; reason: string; evidence: string }>
}
export type CatalogRequest = { action: 'get' | 'status' | 'refresh' | 'recover' | 'cancel' }
  | { action: 'install' | 'schedule'; id: string; version: string }
  | { action: 'auto-check'; enabled: boolean }
  | { action: 'cancel-scheduled' | 'rollback' | 'confirm-update' | 'health' }
  | { action: 'homepage' | 'repository'; id: string; version: string }
export type CatalogView = { autoCheck?: boolean; scheduledInstall?: { id: string; version: string; digest: string }; rollbackScheduled?: boolean;
  pendingUpdate?: { id: string; version: string; previousVersion: string; canConfirm: boolean; recoveryRequired: boolean };
  installed?: Array<{ id: string; version: string; digest: string; catalogState: 'matched' | 'unlisted' | 'unavailable'; review?: Record<string, unknown>; withdrawal?: CatalogEntry['withdrawal']; blocked: boolean }>; entries: CatalogEntry[]; sequence?: number; checkedAt?: string; cached: boolean; error?: string; downloading?: CatalogProgress }
export type CatalogProgress = { id: string; version: string; received: number; total: number; stage: 'checking' | 'downloading' | 'verifying' | 'confirming' | 'preparing'; cancelRequested?: boolean }
export type CatalogReply = { ok: boolean; error?: string; cancelled?: boolean; scheduled?: boolean; catalog?: CatalogView; settings?: Record<string, unknown>; progress?: CatalogProgress }
