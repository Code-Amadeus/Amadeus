const IMAGE_CAPABLE_CHAT_PROVIDERS = new Set(['openai', 'gemini', 'hybrid3'])
const DEEPSEEK_IMAGE_MODELS = new Set([
  'deepseek-flash',
  'deepseek-v4-flash',
  'deepseek-v4-flash-vision-exp',
])

// Hybrid profiles pair the local first-sentence head with a remote model.
const PROFILE_CONNECTIONS: Record<string, string[]> = {
  hybrid: ['hybrid_local', 'bedrock'],
  hybrid2: ['hybrid_local', 'deepseek'],
  hybrid3: ['hybrid_local', 'openai'],
}

/** The model connection groups a main-chat profile needs; a single-model profile needs its own group. */
export function chatProfileConnections(profile: string): string[] {
  return PROFILE_CONNECTIONS[profile] || [profile]
}

/**
 * Profiles to offer for the main chat: those whose connection groups are all configured, plus the current
 * one. Configured is not the same as reachable. Without connection facts (null: offline, or before the first
 * config) every profile stays listed, since an offline choice is still saved for the next start.
 */
export function configuredChatProfiles(profiles: string[], connections: Array<{ id: string; configured?: boolean }> | null, current: string): string[] {
  if (!connections) return profiles.includes(current) || !current ? profiles : [...profiles, current]
  const configured = new Set(connections.filter(group => group.configured === true).map(group => group.id))
  const offered = profiles.filter(profile => profile === current || chatProfileConnections(profile).every(id => configured.has(id)))
  return offered.includes(current) || !current ? offered : [...offered, current]
}

export function chatProviderSupportsImages(provider: unknown, model: unknown = ''): boolean {
  const normalizedProvider = String(provider || '').trim().toLowerCase()
  if (normalizedProvider === 'deepseek' || normalizedProvider === 'hybrid2') {
    return DEEPSEEK_IMAGE_MODELS.has(String(model || '').trim().toLowerCase())
  }
  return IMAGE_CAPABLE_CHAT_PROVIDERS.has(normalizedProvider)
}
