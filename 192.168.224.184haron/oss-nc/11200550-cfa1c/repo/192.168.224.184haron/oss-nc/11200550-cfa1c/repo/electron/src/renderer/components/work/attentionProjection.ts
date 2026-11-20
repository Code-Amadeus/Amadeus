import type { AttentionOption, AttentionRequest } from './types'

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function text(value: unknown): string {
  return value === undefined || value === null ? '' : String(value).trim()
}

function normalizeOption(value: unknown): AttentionOption | null {
  const raw = record(value)
  const id = text(raw.id)
  const label = text(raw.label)
  if (!id || !label) return null
  const entityKind = text(raw.entityKind || raw.entity_kind)
  return {
    id,
    label,
    entityKind: entityKind === 'project' || entityKind === 'work_item' ? entityKind : 'other',
    description: text(raw.description) || undefined,
    parentLabel: text(raw.parentLabel || raw.parent_label) || undefined,
    metadata: record(raw.metadata) as Record<string, string | number | boolean>,
  }
}

export function normalizeAttentionRequest(value: unknown): AttentionRequest | null {
  const raw = record(value)
  const id = text(raw.id)
  const sessionId = text(raw.sessionId || raw.session_id)
  const kind = text(raw.kind)
  const status = text(raw.status)
  const options = (Array.isArray(raw.options) ? raw.options : [])
    .map(normalizeOption)
    .filter((option): option is AttentionOption => option !== null)
  if (!id || !sessionId || kind !== 'selection' || status !== 'pending' || options.length < 2) {
    return null
  }
  return {
    schemaId: text(raw.schemaId || raw.schema_id),
    id,
    sessionId,
    kind: 'selection',
    status: 'pending',
    title: text(raw.title) || 'Needs your choice',
    prompt: text(raw.prompt),
    options,
    createdAt: Number(raw.createdAt || raw.created_at || 0),
    expiresAt: Number(raw.expiresAt || raw.expires_at || 0),
  }
}

export function attentionRequestsFromEnvelope(value: unknown): AttentionRequest[] {
  const envelope = record(value)
  return (Array.isArray(envelope.requests) ? envelope.requests : [])
    .map(normalizeAttentionRequest)
    .filter((request): request is AttentionRequest => request !== null)
}
