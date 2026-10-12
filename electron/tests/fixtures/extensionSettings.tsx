import React from 'react'
import ReactDOM from 'react-dom/client'
import SettingsPage from '../../src/renderer/components/SettingsPage'
import { I18nProvider } from '../../src/renderer/i18n'
import { ThemeProvider } from '../../src/renderer/theme'
import Sidebar from '../../src/renderer/components/Sidebar'
import { validateThemePalette } from '../../src/shared/extensionTheme'
import themeManifest from '../../../examples/extensions/desktop-theme/src/amadeus_example_theme/amadeus-extension.json'
import type { ExtensionSummary, ExtensionUpdate } from '../../src/shared/extensions'
import '../../src/renderer/styles/index.css'

// Synthetic Host snapshots exercise the production Settings owner and fields.
// This fixture never loads an extension, starts a backend or uses credentials.
const host = window as any
const query = new URLSearchParams(location.search)
const locale = query.get('locale') || 'en-US'
const theme = query.get('theme') || 'classic'
const palette = themeManifest.contributes.theme.find(item => item.id === query.get('palette'))
const listeners: Record<string, Set<(payload: Record<string, unknown>) => void>> = {}
const calls: Array<{ method: string; params: unknown }> = []
const errors: string[] = []
window.addEventListener('error', event => errors.push(String(event.message)))
window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)))
window.confirm = () => true
localStorage.setItem('amadeus.ui.locale', locale)
localStorage.setItem('amadeus.ui.theme', theme)
localStorage.setItem('amadeus.settings.section', query.get('section') || 'extensions')
const voice: ExtensionSummary = {
  id: 'org.example.voice', version: '1.0.0', revision: 1, digest: 'a'.repeat(64), enabled: true, pendingRemoval: false,
  values: { voice: 'sample-english', model: 'standard', speed: 1, normalize: true }, secretConfigured: { api_key: true }, providers: [], status: 'enabled', restartRequired: false,
  manifest: { schema: 'amadeus.extension/v0', id: 'org.example.voice', version: '1.0.0', display_name: { en: 'Example English voice', 'zh-CN': '示例英语语音' },
    description: { en: 'Synthetic remote speech package for UI verification.', 'zh-CN': '用于界面验证的模拟远程语音包。' },
    author: { name: 'Example author' }, runtime: { kind: 'python' }, engines: { amadeus_host_api: '>=0.1,<0.2' },
    contributes: { voice_backend: [{ id: 'speech', contract: 'amadeus.voice.tts/v0', role: 'tts', deployment: 'remote', languages: ['en'], streaming: true }] },
    config: { api_key: { type: 'string', secret: true, required: true, title: { en: 'API key', 'zh-CN': 'API 密钥' } },
      voice: { type: 'string', title: { en: 'Voice name', 'zh-CN': '语音名称' }, required: true },
      model: { type: 'enum', title: { en: 'Model', 'zh-CN': '模型' }, options: ['standard', 'premium'] },
      speed: { type: 'number', title: { en: 'Speed', 'zh-CN': '速度' }, min: 0.5, max: 2, default: 1 },
      normalize: { type: 'boolean', title: { en: 'Normalize audio', 'zh-CN': '音频标准化' }, default: true } },
  },
}
const tools: ExtensionSummary = {
  id: 'org.example.tools', version: '1.2.0', revision: 1, digest: 'b'.repeat(64), enabled: false, pendingRemoval: false,
  values: { endpoint: 'https://example.invalid/mcp' }, secretConfigured: {}, providers: [], status: 'disabled', restartRequired: false,
  manifest: { schema: 'amadeus.extension/v0', id: 'org.example.tools', version: '1.2.0', display_name: { en: 'Example Work tools', 'zh-CN': '示例工作工具' },
    description: { en: 'Synthetic declarative MCP package.', 'zh-CN': '模拟声明式 MCP 扩展包。' }, author: { name: 'Example author' },
    runtime: { kind: 'none' }, engines: { amadeus_host_api: '>=0.1,<0.2' },
    contributes: { mcp_server: [{ id: 'service', contract: 'amadeus.mcp-server/v0', transport: 'http', url: '${config.endpoint}' }] },
    config: { endpoint: { type: 'url', title: { en: 'Endpoint', 'zh-CN': '服务地址' }, required: true } },
  },
}
const skill: ExtensionSummary = {
  id: 'org.example.skill', version: '0.1.0', revision: 1, digest: 'c'.repeat(64), enabled: false, pendingRemoval: false,
  values: {}, secretConfigured: {}, providers: [], status: 'disabled', restartRequired: false,
  manifest: { schema: 'amadeus.extension/v0', id: 'org.example.skill', version: '0.1.0', display_name: { en: 'Example handoff Skill', 'zh-CN': '示例交接 Skill' },
    description: { en: 'Prepare a Work handoff with packaged references.', 'zh-CN': '使用随包参考资料整理工作交接。' }, author: { name: 'Example author' },
    runtime: { kind: 'none' }, engines: { amadeus_host_api: '>=0.1,<0.2' },
    contributes: { skill: [{ id: 'handoff', contract: 'amadeus.skill/v0', path: 'skills/handoff-brief' }] },
  },
}
const desktop = {
  platform: 'win32', values: { AMADEUS_UI_LOCALE: locale, AMADEUS_UI_THEME: theme, TTS_BACKEND: 'gpt_sovits', TTS_OUTPUT_LANGUAGE: '日文' } as Record<string, string>,
  sources: { AMADEUS_UI_LOCALE: 'user', AMADEUS_UI_THEME: 'user', TTS_BACKEND: 'user', TTS_OUTPUT_LANGUAGE: 'user' }, locked: {}, secrets: {}, encryptionAvailable: true,
  mcpConnections: [], mcpConnectionsLocked: false, restartRequired: false, pendingKeys: [] as string[], pendingRevisions: {} as Record<string, number>, retired_settings: [],
  extensions: [voice, tools, skill],
}
let runtime = desktop.extensions.map(extension => ({ id: extension.id, version: extension.version, enabled: extension.enabled,
  state: extension.enabled ? 'active' : 'inactive', detail: extension.enabled ? 'Runner is ready' : 'Package is disabled',
  contributions: Object.entries(extension.manifest.contributes).flatMap(([kind, items]) => items.map(item => ({ kind, id: item.id, qualified_id: `${extension.id}/${item.id}`, health: extension.enabled ? 'ready' : 'disabled' }))),
}))
let failSave = false
if (palette) {
  desktop.values.AMADEUS_UI_THEME = `${themeManifest.id}/${palette.id}`
  desktop.extensions.push({ id: themeManifest.id, version: themeManifest.version, revision: 2, appliedRevision: 2, appliedVersion: themeManifest.version,
    digest: 'd'.repeat(64), enabled: true, pendingRemoval: false, application: 'frontend', frontendSuppressed: false, backendStillRunning: false,
    values: {}, secretConfigured: {}, providers: [], status: 'enabled', restartRequired: false, manifest: themeManifest } as ExtensionSummary)
}
const snapshot = () => structuredClone(desktop)
const config = () => ({ tts_backend: desktop.values.TTS_BACKEND, tts_output_language: desktop.values.TTS_OUTPUT_LANGUAGE === '英文' ? 'en' : 'ja', extensions: structuredClone(runtime) })
const emit = () => { for (const listener of listeners['system.config'] || []) listener({ values: config() }) }
const pending = () => { desktop.restartRequired = true; desktop.pendingKeys = ['AMADEUS_EXTENSIONS']; desktop.pendingRevisions.AMADEUS_EXTENSIONS = 1 }
const subscribe = (method: string, listener: (payload: Record<string, unknown>) => void) => {
  (listeners[method] ||= new Set()).add(listener); return () => { listeners[method].delete(listener) }
}
const send = async (method: string): Promise<Record<string, unknown>> => {
  calls.push({ method, params: {} })
  if (method === 'system.get_config') return { values: config() }
  if (method === 'provider.list') return { provider_availability: [], provider_manifests: [
    { provider_id: 'codex', display_name: 'Codex', runtime_kind: 'native', capabilities: { capability_projections: ['mcp_connection', 'agent_skill'] } },
    { provider_id: 'codex_acp', display_name: 'Codex ACP', runtime_kind: 'agent', capabilities: { capability_projections: ['mcp_connection', 'agent_skill'] } },
    { provider_id: 'pi', display_name: 'Pi', runtime_kind: 'native', capabilities: { capability_projections: ['agent_skill'] } },
    { provider_id: 'unsupported', display_name: 'Incompatible agent', runtime_kind: 'native', capabilities: { capability_projections: [] } },
  ], acp_agents: [], provider_configurations: [] }
  if (method === 'capability.list') return { packages: [] }
  throw new Error('Unexpected synthetic RPC: ' + method)
}
host.amadeus = {
  getDesktopTheme: async () => {
    if (!palette) return { selected: theme, base: theme, reason: '' }
    const normalized = validateThemePalette(palette)
    const image = 'background' in palette ? { mime: 'image/png', base64: btoa(String.fromCharCode(...new Uint8Array(await (await fetch('/fixture-theme-background.png')).arrayBuffer()))) } : undefined
    return { selected: desktop.values.AMADEUS_UI_THEME, base: palette.base, reason: '',
      theme: { ...normalized, id: desktop.values.AMADEUS_UI_THEME, version: themeManifest.version, revision: 2, image } }
  },
  onDesktopThemeChanged: () => () => {},
  getDesktopSettings: async () => snapshot(),
  getCompanionPortraitStatus: async () => ({ installed: false, state: 'not_installed', emotionCount: 0, frameCount: 0, detail: '' }),
  setTitleBarTheme: async () => true,
  installExtension: async () => ({ ok: false, cancelled: true }),
  updateExtension: async (update: ExtensionUpdate) => {
    calls.push({ method: 'updateExtension', params: structuredClone(update) })
    if (failSave) return { ok: false, error: 'Synthetic save failure' }
    const saved = desktop.extensions.find(extension => extension.id === update.id)!
    saved.enabled = update.enabled ?? saved.enabled; saved.status = saved.enabled ? 'enabled' : 'disabled'
    Object.assign(saved.values, update.values); saved.providers = update.providers || saved.providers
    for (const [key, value] of Object.entries(update.secrets || {})) saved.secretConfigured[key] = value !== null
    saved.revision = (saved.revision || 1) + 1; saved.restartRequired = true; pending()
    return { ok: true, settings: snapshot() }
  },
  removeExtension: async (id: string, options: unknown) => {
    calls.push({ method: 'removeExtension', params: { id, options } })
    const saved = desktop.extensions.find(extension => extension.id === id)!
    saved.pendingRemoval = true; saved.status = 'pending_removal'; saved.restartRequired = true; pending()
    return { ok: true, settings: snapshot() }
  },
  updateDesktopSettings: async (update: { values: Record<string, string> }) => {
    calls.push({ method: 'updateDesktopSettings', params: structuredClone(update) })
    Object.assign(desktop.values, update.values); pending()
    return { ok: true, settings: snapshot() }
  },
  restartBackend: async () => {
    calls.push({ method: 'restartBackend', params: {} })
    desktop.extensions = desktop.extensions.filter(extension => !extension.pendingRemoval)
    desktop.extensions.forEach(extension => { extension.restartRequired = false })
    runtime = runtime.filter(observed => desktop.extensions.some(extension => extension.id === observed.id)).map(observed => {
      const desired = desktop.extensions.find(extension => extension.id === observed.id)!
      return { ...observed, enabled: desired.enabled, state: desired.enabled ? 'active' : 'inactive', detail: desired.enabled ? 'Runner is ready' : 'Package is disabled',
        contributions: observed.contributions.map(item => ({ ...item, health: desired.enabled ? 'ready' : 'disabled' })) }
    })
    desktop.restartRequired = false; desktop.pendingKeys = []; desktop.pendingRevisions = {}; emit(); return true
  },
}
host.extensionFixture = { calls, errors, snapshot, setFailSave: (value: boolean) => { failSave = value },
  refreshRuntime: () => { emit() } }
document.documentElement.style.height = '100%'
document.body.style.cssText = 'margin:0;height:100%;overflow:hidden'
document.getElementById('root')!.style.cssText = 'height:100%;display:flex'
ReactDOM.createRoot(document.getElementById('root')!).render(<ThemeProvider><I18nProvider>
  {query.get('presentation') === 'plugins' && <Sidebar page="plugins" onNavigate={() => {}} renderActive={false} wallpaperActive={false} onToggleRender={() => {}} onToggleWallpaper={() => {}} />}
  <div className="desktop-main-content flex-1 flex min-w-0"><SettingsPage presentation={query.get('presentation') === 'plugins' ? 'plugins' : 'settings'} send={send} subscribe={subscribe} connected reconnectBackend={async () => {}} /></div>
</I18nProvider></ThemeProvider>)
