/** Minimal CommonJS capability boundary for the sandboxed Slice surface. */

import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('amadeus', {
  toggleCompanionPanel: (workItemId: string): Promise<boolean> => ipcRenderer.invoke('companion.toggle', workItemId),
  getCompanionPanelState: (): Promise<boolean> => ipcRenderer.invoke('companion.state'),
  onCompanionPanelState: (callback: (open: boolean) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, open: boolean) => callback(open)
    ipcRenderer.on('companion.state', listener)
    return () => ipcRenderer.removeListener('companion.state', listener)
  },
  setElectronSliceShape: (
    bounds: Array<{ x: number; y: number; width: number; height: number }>,
  ): Promise<boolean> => ipcRenderer.invoke('electron-slice.set-shape', bounds),
})
