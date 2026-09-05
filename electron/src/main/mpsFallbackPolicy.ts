type Environment = Record<string, string | undefined>

export function defaultMpsFallbackEnvironment(
  platform: NodeJS.Platform,
  architecture: string,
  environment: Environment,
): Environment {
  if (String(environment.PYTORCH_ENABLE_MPS_FALLBACK || '').trim()) return {}

  const backend = String(environment.TTS_BACKEND || 'gpt_sovits').trim().toLowerCase()
  const device = String(environment.TTS_DEVICE || '').trim().toLowerCase()
  const resolvesToMps = !device
    || device === 'auto'
    || device === 'cuda'
    || device.startsWith('mps')

  if (
    platform === 'darwin'
    && architecture === 'arm64'
    && backend === 'gpt_sovits'
    && resolvesToMps
  ) {
    return { PYTORCH_ENABLE_MPS_FALLBACK: '1' }
  }
  return {}
}
