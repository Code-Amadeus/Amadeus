/// <reference types="vite/client" />

export declare global {
  interface WorkPreviewDescriptor {
    previewId: string
    workItemId: string
    attemptId: string
    title: string
    url: string
    revision: number
    contentRevision: number
    status: string
    error: string
    lifecycle: string
    artifactRef: string
    appSessionId: string
    hostSurfaceId: string
    presentedAppSessionId?: string
    presentedHostSurfaceId?: string
    presentationPhase?: 'preview' | 'auip-preloading' | 'auip-attached' | 'auip-closing' | 'auip-conflict' | 'auip-ended'
  }

  interface WorkPreviewIpcResult {
    ok: boolean
    detail: string
    descriptor?: WorkPreviewDescriptor
  }

  interface AmadeusAPI {
    getBackendConnection: () => Promise<{
      url: string
      protocols: string[]
      instanceNonce: string
      authScheme: string
    } | null>
    restartBackend: () => Promise<boolean>
    getDesktopSettings: () => Promise<Record<string, unknown> | null>
    updateDesktopSettings: (update: {
      values?: Record<string, string | boolean | null>
      secrets?: Record<string, string | null>
    }) => Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }>
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
    }) => Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }>
    removeMcpConnection: (connectionId: string) => Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }>
    getChatAvatars: () => Promise<{ user: string; assistant: string } | null>
    selectChatAvatar: (role: 'user' | 'assistant') => Promise<{
      ok: boolean
      cancelled: boolean
      error?: string
      avatars?: { user: string; assistant: string }
    }>
    clearChatAvatar: (role: 'user' | 'assistant') => Promise<{
      ok: boolean
      error?: string
      avatars?: { user: string; assistant: string }
    }>
    focusMainWindow: () => Promise<boolean>
    selectProjectDirectory: () => Promise<{
      ok: boolean
      cancelled: boolean
      path: string
      detail: string
    }>
    openElectronSlice: (bridge: { assetPort: number; bridgePort: number; assetVersion?: string; sliceBounds?: { x: number; y: number; width: number; height: number } }) => Promise<boolean>
    closeElectronSlice: () => Promise<boolean>
    openAuipApp: (launchUrl: string, hostSurfaceId?: string, workItemId?: string) => Promise<{ ok: boolean; detail: string }>
    closeAuipApp: (hostSurfaceId: string, appSessionId?: string) => Promise<{ ok: boolean; status: string; detail: string }>
    openWorkPreview: (descriptor: Record<string, unknown>) => Promise<WorkPreviewIpcResult>
    updateWorkPreview: (descriptor: Record<string, unknown>) => Promise<WorkPreviewIpcResult>
    getWorkPreview: (previewId: string) => Promise<WorkPreviewIpcResult>
    reloadWorkPreview: (previewId: string) => Promise<WorkPreviewIpcResult>
    closeWorkPreview: (previewId: string) => Promise<WorkPreviewIpcResult>
    setWorkPreviewBounds: (previewId: string, bounds: { x: number; y: number; width: number; height: number }) => Promise<boolean>
    onWorkPreviewDescriptor: (listener: (descriptor: WorkPreviewDescriptor) => void) => () => void
    onWorkPreviewLoadState: (listener: (state: Record<string, unknown>) => void) => () => void
    onWorkPreviewCloseRequested: (listener: (state: Record<string, unknown>) => void) => () => void
  }

  interface Window {
    amadeus?: AmadeusAPI
  }
}
