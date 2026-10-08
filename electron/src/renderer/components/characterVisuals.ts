export type VisualBackend = 'sprite' | 'live2d'
export type VisualSurface = 'render' | 'wallpaper'
export type VisualLoadState = 'loading' | 'ready' | 'error' | 'unloaded'

export interface VisualLayout {
  scale: number
  x: number
  y: number
}

export interface VisualProfile {
  profile_id: string
  kind: 'live2d'
  name: string
  model_path: string
  emotion_map: Record<string, string | null>
  mouth: { gain: number; smoothing_ms: number; parameter_ids: string[] }
  layouts: Record<VisualSurface, VisualLayout>
}

export interface VisualConfiguration {
  backend: VisualBackend
  selected_profile_id: string
  core_path: string
  profiles: VisualProfile[]
}

export interface VisualCapabilities {
  expressions: string[]
  lip_sync_ids: string[]
  warnings: string[]
}

export interface VisualRuntimeDiagnostic {
  expression?: string | null
  mouth_ids?: string[]
  warnings?: string[]
  render_texture?: { width: number; height: number; resolution: number } | null
}

export interface VisualSurfaceStatus {
  runtime_id: string
  profile_id: string
  revision: number
  state: VisualLoadState
  error?: string
  diagnostic?: VisualRuntimeDiagnostic | string
}

export interface VisualSnapshot {
  config: VisualConfiguration
  capabilities: VisualCapabilities
  surfaces: Partial<Record<VisualSurface, VisualSurfaceStatus>>
  diagnostic?: string
}

export interface VisualEditor {
  applied: VisualSnapshot | null
  draft: VisualConfiguration | null
}

export function selectedVisualProfile(config: VisualConfiguration | null): VisualProfile | undefined {
  return config?.profiles.find(profile => profile.profile_id === config.selected_profile_id)
}

export function visualConfigurationChanged(editor: VisualEditor): boolean {
  return Boolean(editor.draft && JSON.stringify(editor.draft) !== JSON.stringify(editor.applied?.config))
}

/** Host observations update the applied facts without discarding local edits. */
export function receiveVisualSnapshot(
  editor: VisualEditor,
  snapshot: VisualSnapshot,
  replaceDraft = false,
): VisualEditor {
  return {
    applied: snapshot,
    draft: replaceDraft || !visualConfigurationChanged(editor)
      ? structuredClone(snapshot.config) : editor.draft,
  }
}

/** Replacing artwork retains the visual profile identity and both placements. */
export function inspectedVisualProfile(
  inspected: VisualProfile,
  current?: VisualProfile,
): VisualProfile {
  if (!current) return inspected
  if (current.model_path === inspected.model_path) return current
  return { ...inspected, profile_id: current.profile_id, name: current.name, layouts: current.layouts }
}

/** Only the owned iframe can supply an observation; Host validates its revision. */
export function visualStatusFromFrame(
  source: unknown,
  ownedWindow: unknown,
  data: unknown,
  messageType: 'amadeus.character.status' | 'amadeus.visual.preview.status',
): VisualSurfaceStatus | null {
  if (!ownedWindow || source !== ownedWindow || !data || typeof data !== 'object') return null
  const message = data as { type?: unknown; status?: unknown }
  if (message.type !== messageType || !message.status || typeof message.status !== 'object') return null
  const status = message.status as VisualSurfaceStatus
  if (!['loading', 'ready', 'error', 'unloaded'].includes(status.state)
    || typeof status.runtime_id !== 'string' || !status.runtime_id
    || typeof status.profile_id !== 'string' || !Number.isInteger(status.revision)) return null
  return status
}
