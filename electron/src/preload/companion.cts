/** Presentation-only window: no backend credentials or application execution APIs. */
import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('companion', {
  portraits: () => ipcRenderer.invoke('companion.portraits'),
  close: () => ipcRenderer.invoke('companion.close'),
  dock: () => ipcRenderer.invoke('companion.dock'),
  connected: (connected: boolean) => ipcRenderer.invoke('companion.connected', connected),
})
