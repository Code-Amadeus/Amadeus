/** The Host resolves identity and scope. The panel only carries its current Session binding. */
export type MemoryScope = 'character' | 'shared' | 'project'
export type MemoryCursor = [number, string]
export interface MemoryContext {
  session_id: string
  character_id: string
  project_id: string | null
  context_token: string
}
export interface MemorySource {
  session_id: string
  message_id: string
  revision: number
  incarnation?: string
  projection_version: string
  role: string
  text: string
  created_at_ms?: number
  utc_offset_minutes?: number
}
export interface MemorySourceSpan {
  session_id: string
  message_id: string
  revision: number
  incarnation?: string
  projection_version: string
  start_cp: number
  end_cp: number
}
export interface SavedMemory {
  memory_id: string
  row_version: number
  text: string
  scope: { kind: MemoryScope }
  state: string
  index_state?: string
  created_at_ms?: number
  updated_at_ms?: number
  sources?: MemorySourceSpan[]
  source?: MemorySourceSpan
}
export interface MemoryStatus {
  state?: string
  semantic_state?: string
  semantic_reason?: string
  cleanup_state?: string
  automatic_admission?: 'ready' | 'gate_required' | 'semantic_unavailable'
  automatic_threshold?: number | null
  source_policy_state?: 'ready' | 'recovery_required'
  source_policy_reason?: string
}
export interface MemoryAttempt {
  method: 'memory.remember' | 'memory.correct' | 'memory.forget' | 'memory.review_candidate'
  params: Readonly<Record<string, unknown>>
}
export interface MemorySuggestion {
  candidate_id: string
  row_version: number
  kind: string
  scope: MemoryScope
  created_at_ms: number
  savable: boolean
}
export interface MemorySuggestionDetail extends MemorySuggestion {
  text: string
  condition: string | null
  quotes: { speaker_role: string; text: string }[]
}
export interface MemoryForgetPreview {
  targets: { memory_id: string; row_version: number }[]
  memories: SavedMemory[]
  source_exclusions: MemorySourceSpan[]
}
export type MemorySend = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>

export class MemoryPanelFailure extends Error {
  code: string
  definite: boolean
  reason?: string
  constructor(code: string, definite = false, reason?: string) {
    super(code); this.code = code; this.definite = definite; this.reason = reason
  }
}

/** A closed panel binding cannot send a request or accept a late response in another Session. */
export class MemoryPanelConnection {
  active = true
  context: MemoryContext | null = null
  private sessionId: string
  private send: MemorySend
  constructor(sessionId: string, send: MemorySend) { this.sessionId = sessionId; this.send = send }
  dispose() { this.active = false; this.context = null }
  async request(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    if (!this.active) throw new MemoryPanelFailure('detached', true)
    const context = this.context
    if (method !== 'memory.status' && !context) throw new MemoryPanelFailure('context_changed', true)
    const result = await this.send(method, { ...params, session_id: this.sessionId,
      ...(method === 'memory.status' ? {} : { context_token: context!.context_token }) })
    if (!this.active) throw new MemoryPanelFailure('detached', true)
    if (result.ok !== true) {
      const code = typeof result.error === 'string' ? result.error : 'unknown'
      if (['stale_session', 'context_changed', 'disabled', 'unavailable'].includes(code)) this.context = null
      const definite = ['disabled', 'unavailable', 'stale_session', 'context_changed', 'conflict', 'not_found', 'invalid_request'].includes(code)
      throw new MemoryPanelFailure(code, definite, typeof result.reason === 'string' ? result.reason : undefined)
    }
    const received = result.context as MemoryContext | undefined
    if (!received?.context_token || received.session_id !== this.sessionId
      || method !== 'memory.status' && received.context_token !== context!.context_token) {
      this.context = null
      throw new MemoryPanelFailure('context_changed')
    }
    if (method === 'memory.status') this.context = received
    return result
  }
}

/** Construct once on submit; retries use this same immutable body and these same identities. */
export function memoryAttempt(method: MemoryAttempt['method'], params: Record<string, unknown>,
  newId: () => string = () => crypto.randomUUID()): MemoryAttempt {
  return Object.freeze({ method, params: Object.freeze({ ...structuredClone(params), operation_id: newId(), item_id: newId() }) })
}

export function suggestionKindText(kind: string): string {
  switch (kind) {
    case 'fact': return 'Suggested fact'
    case 'preference': return 'Suggested preference'
    case 'instruction': return 'Suggested instruction'
    case 'event': return 'Suggested event'
    case 'comparison': return 'Suggested comparison'
    default: return 'Memory suggestion'
  }
}

export function sourceSpan(source: MemorySource, start: number, end: number): MemorySourceSpan | null {
  const length = Array.from(source.text).length
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end <= start || end > length) return null
  return { session_id: source.session_id, message_id: source.message_id, revision: source.revision,
    ...(source.incarnation === undefined ? {} : { incarnation: source.incarnation }),
    projection_version: source.projection_version, start_cp: start, end_cp: end }
}

export function sourceTextParts(source: MemorySource, start: number, end: number): [string, string, string] {
  const points = Array.from(source.text)
  return [points.slice(0, start).join(''), points.slice(start, end).join(''), points.slice(end).join('')]
}

/** Textarea offsets use UTF-16 and normalize line endings; Host spans use canonical codepoints. */
export function selectedSourceSpan(source: MemorySource, startUtf16: number, endUtf16: number): MemorySourceSpan | null {
  const points = Array.from(source.text)
  const boundaries = new Map<number, number>([[0, 0]])
  let displayOffset = 0
  for (let index = 0; index < points.length; index++) {
    const point = points[index]
    displayOffset += point.length
    if (point === '\r' && points[index + 1] === '\n') index++
    boundaries.set(displayOffset, index + 1)
  }
  const start = boundaries.get(startUtf16), end = boundaries.get(endUtf16)
  return start === undefined || end === undefined ? null : sourceSpan(source, start, end)
}

// Never render an unknown backend message: it may contain internal paths or exception details.
export function memoryErrorText(error: unknown): string {
  if (error instanceof MemoryPanelFailure && error.reason === 'forget_linked_scope_unavailable')
    return 'A linked memory is outside this context. Forget could not proceed.'
  if (error instanceof MemoryPanelFailure && ['forget_preview_stale', 'forget_confirmation_required'].includes(error.reason || ''))
    return 'Linked memories changed. Inspect the latest memory and review the forget list again.'
  switch (error instanceof MemoryPanelFailure ? error.code : '') {
    case 'disabled': return 'Memory is disabled in this backend.'
    case 'unavailable': return 'Memory is unavailable. Check that Eidema is installed and configured.'
    case 'stale_session':
    case 'context_changed': return 'The chat context changed. Refresh Memory before continuing.'
    case 'conflict': return 'This memory changed or this operation conflicts with an earlier request. Refresh and inspect the latest version.'
    case 'not_found': return 'This memory is no longer available. Refresh the list.'
    case 'invalid_request': return 'This memory request could not be accepted. Check the entry and source range.'
    case 'busy': return 'Memory is busy. Try again shortly.'
    case 'outcome_unknown': return 'The operation may still commit. Retry the same operation to confirm its outcome.'
    default: return 'Could not confirm the memory operation. Check the connection and try again.'
  }
}

export function memoryIndexText(state?: string): string {
  switch (state) {
    case 'ready': return 'Semantic index ready'
    case 'pending':
    case 'warming':
    case 'indexing': return 'Semantic indexing pending'
    case 'partial': return 'Semantic recall partially indexed; coverage is incomplete'
    case 'failed': return 'Semantic indexing failed; lexical recall only'
    case 'unavailable':
    case 'disabled': return 'Semantic recall unavailable; lexical recall only'
    default: return 'Semantic readiness unknown'
  }
}

export function memorySemanticReasonText(reason?: string): string {
  switch (reason) {
    case 'warming': return 'The semantic model is warming up. You can manage memories while it loads.'
    case 'model_missing': return 'The configured local semantic model is missing. Install it through backend setup; it will not be downloaded automatically.'
    case 'model_corrupt': return 'The local semantic model failed file verification. Prepare the pinned model files again through backend setup.'
    case 'model_unsupported': return 'The local semantic model or runtime version is unsupported. Use the pinned versions from backend setup.'
    case 'dependencies_missing': return 'Semantic model dependencies are missing from this backend.'
    case 'index_pending': return 'Some saved memories are waiting for semantic indexing.'
    case 'embedding_failed': return 'Semantic indexing encountered an error. Saved memories remain durable.'
    default: return ''
  }
}

export function memoryAutomaticAdmissionText(state?: string): string {
  switch (state) {
    case 'ready': return 'Automatic recall ready'
    case 'gate_required': return 'Automatic use of memories in chat is awaiting validation.'
    case 'semantic_unavailable': return 'Automatic recall unavailable; saved memories remain manageable'
    default: return ''
  }
}

export function memorySourcePolicyReasonText(reason?: string): string {
  switch (reason) {
    case 'starting': return 'Memory source protections are still starting.'
    case 'identity_mismatch': return 'The memory database belongs to another user identity.'
    case 'core_unreadable': return 'The memory database cannot be read safely.'
    case 'core_missing': return 'The expected memory database is missing.'
    case 'store_rollback': return 'The memory database is older than its recorded source protections.'
    case 'invalid_coordinates': return 'Saved memory source ranges could not be verified.'
    case 'sidecar_rebuild_required': return 'The derived memory source-protection file needs rebuilding.'
    case 'publication_pending': return 'A memory source-protection update has not finished.'
    default: return ''
  }
}

export function memorySourcePolicyActionText(reason?: string): string {
  switch (reason) {
    case 'starting': return 'Refresh shortly to check memory source protections.'
    case 'identity_mismatch': return 'Restore the original configured user identity, or use a separate database and Session namespace for this identity.'
    case 'core_unreadable':
    case 'core_missing':
    case 'store_rollback': return 'Stop the backend. Restore matching database and policy backups, or configure a separate database and Session namespace. Keep the original files.'
    case 'invalid_coordinates': return 'Stop the backend and run the recovery command to verify the saved source ranges. Invalid authoritative ranges cannot be repaired by rebuilding. If recovery refuses, restore matching verified database and policy backups or isolate this namespace; keep the original files.'
    case 'sidecar_rebuild_required':
    case 'publication_pending': return 'Stop the backend, then run the recovery command with its configured database and user identity. It preserves the original files and refuses unsafe recovery. If it refuses, restore matching backups or isolate this namespace; restart only after recovery.'
    default: return 'Stop the backend and inspect its memory configuration. Preserve the original files before restoring matching backups or isolating this database and Session namespace.'
  }
}

export const memoryPolicyRecoveryCommand = 'uv run --locked --no-sync python tools/recover_memory_policy.py --db <configured-db> --user-id <configured-user-id>'

export function memoryLifecycleText(state: string): string {
  switch (state) {
    case 'active': return 'Current'
    case 'superseded': return 'Superseded by a change'
    case 'retracted': return 'Retracted as an error'
    case 'expired': return 'Expired'
    case 'forgotten': return 'Forgotten'
    default: return 'Historical version'
  }
}
