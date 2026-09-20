const IMAGE_CAPABLE_CHAT_PROVIDERS = new Set(['openai', 'gemini', 'hybrid3'])
const DEEPSEEK_IMAGE_MODELS = new Set([
  'deepseek-flash',
  'deepseek-v4-flash',
  'deepseek-v4-flash-vision-exp',
])

export function chatProviderSupportsImages(provider: unknown, model: unknown = ''): boolean {
  const normalizedProvider = String(provider || '').trim().toLowerCase()
  if (normalizedProvider === 'deepseek' || normalizedProvider === 'hybrid2') {
    return DEEPSEEK_IMAGE_MODELS.has(String(model || '').trim().toLowerCase())
  }
  return IMAGE_CAPABLE_CHAT_PROVIDERS.has(normalizedProvider)
}
