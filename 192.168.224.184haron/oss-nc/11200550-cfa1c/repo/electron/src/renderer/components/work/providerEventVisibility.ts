type ProviderEventLike = {
  type?: unknown
}

const INTERNAL_PROVIDER_EVENT_TYPES = new Set([
  'context.delivered',
])

export function isVisibleProviderEvent(event: ProviderEventLike): boolean {
  return !INTERNAL_PROVIDER_EVENT_TYPES.has(String(event.type || '').trim().toLowerCase())
}

export function visibleProviderEvents<T extends ProviderEventLike>(events: readonly T[]): T[] {
  return events.filter(isVisibleProviderEvent)
}
