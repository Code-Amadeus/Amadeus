import type { ModelConnectionCatalogGroup } from './modelConnectionCatalog'
import { catalogConfiguration, desktopCatalogFields } from '../../shared/configCatalog.js'

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
  snapshot?: { values?: Record<string, string> } | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const profile = values.GRAPHICS_PROFILE ?? runtime?.profile ?? String(desktopCatalogFields.GRAPHICS_PROFILE.default)
  const customFps = values.RENDER_MAX_FPS ?? runtime?.custom_max_fps ?? Number(desktopCatalogFields.RENDER_MAX_FPS.default)
  const selectedFps = profile === 'standard' ? 60 : profile === 'power_saving' ? 30 : Number(customFps)
  // Preserve an explicit running choice only while describing that same profile.
  // An unset value otherwise follows the selected preset, including offline edits.
  const sampling = runtime && profile === runtime.profile && selectedFps === runtime.effective_max_fps
    ? runtime.texture_sampling : selectedFps === 60
  const budget = catalogConfiguration('graphics_budget', snapshot, {
    GRAPHICS_PROFILE: profile,
    RENDER_MAX_FPS: customFps,
    RENDER_MAX_RESOLUTION: runtime?.custom_max_resolution ?? Number(desktopCatalogFields.RENDER_MAX_RESOLUTION.default),
  })
  return [{
    ...budget,
    active: false, configured: true,
    status: profile === 'standard' ? 'Standard' : profile === 'power_saving' ? 'Power saving' : 'Custom',
    status_ok: true,
    fields: budget.fields.filter(field => profile === 'custom' || field.key === 'GRAPHICS_PROFILE'),
  }, {
    ...catalogConfiguration('graphics_sampling', snapshot, {
      RENDER_TEXTURE_SAMPLING: sampling,
      ...(runtime?.bc7_cache !== undefined ? { RENDER_BC7_CACHE: runtime.bc7_cache } : {}),
    }),
    active: false, configured: true, status: 'Configured', status_ok: true,
  }]
}
