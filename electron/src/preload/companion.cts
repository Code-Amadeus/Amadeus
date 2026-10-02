/** Card controls forward bounded input actions; no credentials or execution APIs. */
import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('companion', {
  portraits: () => ipcRenderer.invoke('companion.portraits'),
  close: () => ipcRenderer.invoke('companion.close'),
  input: (action: 'status' | 'voice_start' | 'voice_stop' | 'vision_toggle') => ipcRenderer.invoke('companion.input', action),
  fitContent: (height: number) => ipcRenderer.invoke('companion.fit-content', height),
  connected: (connected: boolean) => ipcRenderer.invoke('companion.connected', connected),
})
