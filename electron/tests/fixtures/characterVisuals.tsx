import React, { useState } from 'react'
import ReactDOM from 'react-dom/client'
import SettingsPage from '../../src/renderer/components/SettingsPage'
import Sidebar from '../../src/renderer/components/Sidebar'
import { I18nProvider } from '../../src/renderer/i18n'
import { ThemeProvider } from '../../src/renderer/theme'
import type { Page } from '../../src/renderer/App'
import type { VisualConfiguration, VisualProfile, VisualSnapshot } from '../../src/renderer/components/characterVisuals'
import '../../src/renderer/styles/index.css'

// Settings navigation and the complete visual editor are production components.
// Host controls are fixtures; previews load the production Cubism runtime and
// caller-supplied local model/Core without copying assets.
const host = window as any
const seed = host.visualSeed
const listeners: Record<string, Set<(payload: Record<string, unknown>) => void>> = {}
let config: VisualConfiguration = { backend: 'sprite', selected_profile_id: '', core_path: '', profiles: [] }
let revision = 0, reloadRevision = 0
const calls: Array<{ method: string; params: Record<string, unknown> }> = []
const errors: string[] = []
window.addEventListener('error', event => errors.push(String(event.message)))
window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)))
localStorage.setItem('amadeus.ui.locale', seed.locale || 'zh-CN')
localStorage.setItem('amadeus.ui.theme', 'classic')
localStorage.setItem('amadeus.sidebar.collapsed', '0')
localStorage.setItem('amadeus.settings.section', 'capabilities')
const desktop = {
  platform: 'win32', values: { AMADEUS_UI_LOCALE: seed.locale || 'zh-CN', AMADEUS_UI_THEME: 'classic' },
  sources: {}, locked: {}, secrets: {}, encryptionAvailable: false,
  mcpConnections: [], mcpConnectionsLocked: false, restartRequired: false,
  pendingKeys: [], pendingRevisions: {}, retired_settings: [],
}
const snapshot = (): VisualSnapshot => ({ config: structuredClone(config), capabilities: seed.capabilities, surfaces: {} })
const emit = (method: string, payload: Record<string, unknown>) => {
  for (const listener of listeners[method] || []) listener(payload)
}
const subscribe = (method: string, listener: (payload: Record<string, unknown>) => void) => {
  (listeners[method] ||= new Set()).add(listener)
  return () => { listeners[method].delete(listener) }
}
const send = async (method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> => {
  calls.push({ method, params: structuredClone(params) })
  if (method === 'character.list') return { characters: [{ character_id: 'kurisu', name: 'Kurisu', persona: '', valid: true, builtin: true, editable: false }], active: { character_id: 'kurisu', name: 'Kurisu' }, limits: { name_max_chars: 128, persona_max_chars: 7900 } }
  if (method === 'system.get_config') return { values: {} }
  if (method === 'provider.list') return { provider_availability: [], provider_manifests: [], acp_agents: [], provider_configurations: [] }
  if (method === 'capability.list') return { packages: [] }
  if (method === 'visual.get') return snapshot() as unknown as Record<string, unknown>
  if (method === 'visual.inspect') {
    if (params.model_path !== seed.profile.model_path) throw new Error('Fixture model path does not exist')
    return { profile: { ...structuredClone(seed.profile), profile_id: crypto.randomUUID() }, capabilities: seed.capabilities }
  }
  if (method === 'visual.save') {
    config = structuredClone(params.config as VisualConfiguration)
    revision += 1
    const applied = snapshot()
    emit('visual.updated', applied as unknown as Record<string, unknown>)
    return applied as unknown as Record<string, unknown>
  }
  if (method === 'visual.reload') {
    revision += 1
    reloadRevision += 1
    return snapshot() as unknown as Record<string, unknown>
  }
  if (method === 'visual.preview') {
    const profile = params.profile as VisualProfile
    const surface = params.surface === 'wallpaper' ? 'wallpaper' : 'render'
    return {
      url: seed.preview_url, capabilities: seed.capabilities,
      config: {
        backend: 'live2d', surface, runtime_id: 'visual-gui-smoke', profile_id: profile.profile_id, revision, reload_revision: reloadRevision,
        model_url: seed.model_url, core_url: seed.core_url,
        emotion_map: profile.emotion_map, mouth: profile.mouth, layout: profile.layouts[surface], warnings: seed.capabilities.warnings,
      },
    }
  }
  throw new Error('Unexpected smoke RPC: ' + method)
}
host.amadeus = {
  getDesktopSettings: async () => desktop,
  getCompanionPortraitStatus: async () => ({ installed: false, state: 'not_installed', emotionCount: 0, frameCount: 0, detail: '' }),
  getChatAvatars: async () => ({ user: '', assistant: '' }),
  setTitleBarTheme: async () => true,
  selectVisualFile: async (kind: string) => ({ ok: true, cancelled: false, detail: '',
    path: kind === 'core' ? seed.core_path : seed.profile.model_path }),
}
host.visualFixture = { calls, errors, emit, snapshot }
document.documentElement.style.height = '100%'
document.body.style.cssText = 'margin:0;height:100%;overflow:hidden'
document.getElementById('root')!.style.cssText = 'height:100%;display:flex'

function SettingsNavigationFixture() {
  const [page, setPage] = useState<Page>('chat')
  return <>
    <Sidebar page={page} onNavigate={setPage} renderActive={false} wallpaperActive={false}
      onToggleRender={() => {}} onToggleWallpaper={() => {}} />
    <div style={{ display: 'flex', flex: 1, minWidth: 0, minHeight: 0 }}>
      {page === 'settings' ? <SettingsPage send={send} subscribe={subscribe} connected reconnectBackend={async () => {}} />
        : <div style={{ padding: 28 }}>Settings navigation fixture</div>}
    </div>
  </>
}
ReactDOM.createRoot(document.getElementById('root')!).render(
  <ThemeProvider><I18nProvider><SettingsNavigationFixture /></I18nProvider></ThemeProvider>,
)
