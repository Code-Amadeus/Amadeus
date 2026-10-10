import type { ModelConnectionCatalogGroup } from './modelConnectionCatalog'
import { catalogConfiguration } from '../../shared/configCatalog.js'
import type { StartupSnapshot } from '../../shared/startupSettings.js'

export interface GraphicsRuntimeSettings {
  profile: string
  custom_max_fps: number
  custom_max_resolution: number
  texture_sampling: boolean
  bc7_cache?: boolean
  effective_max_fps: number
  effective_max_resolution: number | null
}

export function buildGraphicsConfiguration(
  runtime?: GraphicsRuntimeSettings,
  snapshot?: StartupSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const budget = catalogConfiguration('graphics_budget', snapshot, {
    GRAPHICS_PROFILE: runtime?.profile,
    RENDER_MAX_FPS: runtime?.custom_max_fps,
    RENDER_MAX_RESOLUTION: runtime?.custom_max_resolution,
  })
  const value = (key: string) => budget.fields.find(field => field.key === key)?.value
  const profile = value('GRAPHICS_PROFILE')
  const customFps = value('RENDER_MAX_FPS')
  const selectedFps = profile === 'standard' ? 60 : profile === 'power_saving' ? 30
    : profile === 'custom' && customFps !== undefined ? Number(customFps) : undefined
  // Preserve an explicit running choice only while describing that same profile.
  // An unset value otherwise follows the selected preset, including offline edits.
  const sampling = runtime && profile === runtime.profile && selectedFps === runtime.effective_max_fps
    && !snapshot?.pendingRevisions?.RENDER_TEXTURE_SAMPLING
    ? runtime.texture_sampling : selectedFps === undefined ? undefined : selectedFps === 60
  return [{
    ...budget,
    active: false, configured: true,
    status: profile === undefined ? 'Backend status unavailable'
      : profile === 'standard' ? 'Standard' : profile === 'power_saving' ? 'Power saving' : 'Custom',
    status_ok: true,
    fields: budget.fields.filter(field => profile === undefined || profile === 'custom' || field.key === 'GRAPHICS_PROFILE'),
  }, {
    ...catalogConfiguration('graphics_sampling', snapshot, {
      RENDER_TEXTURE_SAMPLING: sampling,
      ...(runtime?.bc7_cache !== undefined ? { RENDER_BC7_CACHE: runtime.bc7_cache } : {}),
    }),
    active: false, configured: true, status: 'Configured', status_ok: true,
  }]
}
