/** Capability boundary for the floating companion surface. */

import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('amadeus', {
  getBackendConnection: () => ipcRenderer.invoke('get-backend-connection'),
  focusMainWindow: (): Promise<boolean> => ipcRenderer.invoke('main-window.focus'),
  openCodexThread: (threadId: string): Promise<boolean> => ipcRenderer.invoke('floating-companion.open-codex', threadId),
  closeFloatingCompanion: (): Promise<boolean> => ipcRenderer.invoke('floating-companion.close'),
  getFloatingCompanionDisplay: () => ipcRenderer.invoke('floating-companion.display'),
  onFloatingCompanionDisplayChanged: (listener: () => void) => {
    ipcRenderer.on('floating-companion.display-changed', listener)
    return () => ipcRenderer.removeListener('floating-companion.display-changed', listener)
  },
  setFloatingCompanionHitRegions: (
    bounds: Array<{ x: number; y: number; width: number; height: number }>,
  ): Promise<boolean> => ipcRenderer.invoke('floating-companion.set-hit-regions', bounds),
})
