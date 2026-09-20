/**
 * Preload script — exposes minimal backend IPC to the renderer via contextBridge.
 */

import { contextBridge, ipcRenderer } from 'electron'

type WorkPreviewBounds = { x: number; y: number; width: number; height: number }
type WorkPreviewListener = (payload: Record<string, unknown>) => void

contextBridge.exposeInMainWorld('amadeus', {
  getBackendConnection: (): Promise<{
    url: string
    protocols: string[]
    instanceNonce: string
    authScheme: string
  } | null> => ipcRenderer.invoke('get-backend-connection'),
  restartBackend: (): Promise<boolean> => ipcRenderer.invoke('restart-backend'),
  getDesktopSettings: (): Promise<Record<string, unknown> | null> => ipcRenderer.invoke('desktop-settings.get'),
  updateDesktopSettings: (update: {
    values?: Record<string, string | boolean | null>
    secrets?: Record<string, string | null>
  }): Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }> => ipcRenderer.invoke('desktop-settings.update', update),
  upsertMcpConnection: (update: {
    connection: {
      id?: string
      name: string
      enabled?: boolean
      transport: 'stdio' | 'http'
      providerIds?: string[]
      command?: string
      arguments?: string[]
      cwd?: string
      url?: string
      bearerTokenEnvVar?: string
    }
    environment?: Record<string, string | null>
    clearEnvironment?: boolean
  }): Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }> => ipcRenderer.invoke('mcp-connections.upsert', update),
  removeMcpConnection: (connectionId: string): Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }> => ipcRenderer.invoke('mcp-connections.remove', connectionId),
  getChatAvatars: (): Promise<{ user: string; assistant: string } | null> => ipcRenderer.invoke('chat-avatars.get'),
  selectChatAvatar: (role: 'user' | 'assistant'): Promise<{ ok: boolean; cancelled: boolean; error?: string; avatars?: { user: string; assistant: string } }> => ipcRenderer.invoke('chat-avatars.select', role),
  clearChatAvatar: (role: 'user' | 'assistant'): Promise<{ ok: boolean; error?: string; avatars?: { user: string; assistant: string } }> => ipcRenderer.invoke('chat-avatars.clear', role),
  focusMainWindow: (): Promise<boolean> => ipcRenderer.invoke('main-window.focus'),
  selectProjectDirectory: (): Promise<{ ok: boolean; cancelled: boolean; path: string; detail: string }> => ipcRenderer.invoke('project-directory.select'),
  openElectronSlice: (bridge: { assetPort: number; bridgePort: number; assetVersion?: string; sliceBounds?: { x: number; y: number; width: number; height: number } }): Promise<boolean> => ipcRenderer.invoke('electron-slice.open', bridge),
  closeElectronSlice: (): Promise<boolean> => ipcRenderer.invoke('electron-slice.close'),
  openAuipApp: (launchUrl: string, hostSurfaceId?: string, workItemId?: string): Promise<{ ok: boolean; detail: string }> => ipcRenderer.invoke('auip-app.open', launchUrl, hostSurfaceId, workItemId),
  closeAuipApp: (hostSurfaceId: string, appSessionId?: string): Promise<{ ok: boolean; status: string; detail: string }> => ipcRenderer.invoke('auip-app.close', hostSurfaceId, appSessionId),
  openWorkOverlay: (): Promise<boolean> => ipcRenderer.invoke('work-overlay.open'),
  closeWorkOverlay: (): Promise<boolean> => ipcRenderer.invoke('work-overlay.close'),
  setWorkOverlayMouseIgnore: (ignore: boolean): Promise<boolean> => ipcRenderer.invoke('work-overlay.set-mouse-ignore', ignore),
  setWorkOverlayPanelBounds: (bounds: { x: number; y: number; width: number; height: number }): Promise<boolean> => ipcRenderer.invoke('work-overlay.set-panel-bounds', bounds),
  setWorkOverlayHitRegions: (bounds: Array<{ x: number; y: number; width: number; height: number }>): Promise<boolean> => ipcRenderer.invoke('work-overlay.set-hit-regions', bounds),
  openWorkPreview: (descriptor: Record<string, unknown>) => ipcRenderer.invoke('work-preview.open', descriptor),
  updateWorkPreview: (descriptor: Record<string, unknown>) => ipcRenderer.invoke('work-preview.update', descriptor),
  getWorkPreview: (previewId: string) => ipcRenderer.invoke('work-preview.get', previewId),
  reloadWorkPreview: (previewId: string) => ipcRenderer.invoke('work-preview.reload', previewId),
  closeWorkPreview: (previewId: string) => ipcRenderer.invoke('work-preview.close', previewId),
  setWorkPreviewBounds: (previewId: string, bounds: WorkPreviewBounds): Promise<boolean> => ipcRenderer.invoke('work-preview.set-bounds', previewId, bounds),
  onWorkPreviewDescriptor: (listener: WorkPreviewListener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: Record<string, unknown>) => listener(payload)
    ipcRenderer.on('work-preview.descriptor', handler)
    return () => ipcRenderer.removeListener('work-preview.descriptor', handler)
  },
  onWorkPreviewLoadState: (listener: WorkPreviewListener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: Record<string, unknown>) => listener(payload)
    ipcRenderer.on('work-preview.load-state', handler)
    return () => ipcRenderer.removeListener('work-preview.load-state', handler)
  },
  onWorkPreviewCloseRequested: (listener: WorkPreviewListener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: Record<string, unknown>) => listener(payload)
    ipcRenderer.on('work-preview.close-requested', handler)
    return () => ipcRenderer.removeListener('work-preview.close-requested', handler)
  },
})
