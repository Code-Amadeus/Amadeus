import type { ModelConnectionCatalogField, ModelConnectionCatalogGroup } from './modelConnectionCatalog'

export interface GraphicsRuntimeSettings {
  profile: string
  custom_max_fps: number
  custom_max_resolution: number
  texture_sampling: boolean
  effective_max_fps: number
  effective_max_resolution: number | null
}

export function buildGraphicsConfiguration(
  runtime?: GraphicsRuntimeSettings,
  snapshot?: { values?: Record<string, string> } | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const profile = values.GRAPHICS_PROFILE ?? runtime?.profile ?? 'standard'
  const field = (key: string, label: string, type: ModelConnectionCatalogField['type'],
    value: string | boolean, extra: Partial<ModelConnectionCatalogField> = {}): ModelConnectionCatalogField => ({
    key, label, type, value, editable: true, restart_required: true, ...extra,
  })
  const fields = [field('GRAPHICS_PROFILE', 'Graphics profile', 'select', profile, {
    options: [
      { value: 'standard', label: 'Standard · 60 FPS, native pixel density' },
      { value: 'power_saving', label: 'Power saving · 30 FPS, up to 1.5× pixel density' },
      { value: 'custom', label: 'Custom' },
    ],
    description: 'Choose animation smoothness and GPU load. Custom values are preserved when using a preset.',
  })]
  if (profile === 'custom') fields.push(
    field('RENDER_MAX_FPS', 'Frame-rate limit', 'number', values.RENDER_MAX_FPS ?? String(runtime?.custom_max_fps ?? 30), {
      min: 10, max: 240, step: 1, description: '10–240 FPS. This is a ceiling, not a guaranteed frame rate.',
    }),
    field('RENDER_MAX_RESOLUTION', 'Pixel-density limit', 'number', values.RENDER_MAX_RESOLUTION ?? String(runtime?.custom_max_resolution ?? 1.5), {
      min: 0.25, max: 4, step: 0.25,
      description: '0.25–4.0×, capped by the display’s native pixel density. Lower values reduce GPU work but may soften the image.',
    }),
  )
  return [{
    id: 'graphics_budget', label: 'Rendering budget',
    description: 'Shared by character rendering and wallpaper surfaces.',
    active: false, configured: true,
    status: profile === 'standard' ? 'Standard' : profile === 'power_saving' ? 'Power saving' : 'Custom',
    status_ok: true, fields,
  }, {
    id: 'graphics_sampling', label: 'Experimental texture sampling',
    description: 'Load fewer animation source frames to reduce texture memory. Keep off for the original full-frame loading behavior.',
    active: false, configured: true, status: 'Experimental', status_ok: false,
    fields: [field('RENDER_TEXTURE_SAMPLING', 'Sample animation textures', 'boolean',
      values.RENDER_TEXTURE_SAMPLING === undefined ? runtime?.texture_sampling ?? false : values.RENDER_TEXTURE_SAMPLING === 'true', {
        description: 'Independent of the graphics profile. Requires a backend restart and reopening the character or wallpaper to rebuild textures.',
      })],
  }]
}
