const IMAGE_CAPABLE_CHAT_PROVIDERS = new Set(['openai', 'gemini', 'hybrid3'])

export function chatProviderSupportsImages(provider: unknown): boolean {
  return IMAGE_CAPABLE_CHAT_PROVIDERS.has(String(provider || '').trim().toLowerCase())
}
