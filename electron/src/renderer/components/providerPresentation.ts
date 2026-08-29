type ProviderManifestView = {
  provider_id: string
  selection_priority: number
}

function manifestViews(value: unknown): ProviderManifestView[] {
  if (!Array.isArray(value)) return []
  return value
    .filter(item => item && typeof item === 'object')
    .map(item => {
      const row = item as Record<string, unknown>
      return {
        provider_id: String(row.provider_id || ''),
        selection_priority: Number(row.selection_priority || 0),
      }
    })
    .filter(item => item.provider_id)
}

/** Choose a presentation default from the Provider contract, never its brand. */
export function preferredProvider(
  providers: string[],
  manifests: unknown,
): string {
  const available = new Set(providers)
  const ranked = manifestViews(manifests)
    .filter(item => available.has(item.provider_id))
    .sort((left, right) => (
      right.selection_priority - left.selection_priority
      || left.provider_id.localeCompare(right.provider_id)
    ))
  return ranked[0]?.provider_id || providers[0] || ''
}

export function preserveOrChooseProvider(
  current: string,
  providers: string[],
  manifests: unknown,
): string {
  return providers.includes(current)
    ? current
    : preferredProvider(providers, manifests)
}
