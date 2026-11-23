/** Minimal CommonJS capability boundary for the sandboxed Slice surface. */

import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('amadeus', {
  focusMainWindow: (): Promise<boolean> => ipcRenderer.invoke('main-window.focus'),
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
  getControlPositions: (): Promise<Record<string, { x: number; y: number }>> =>
    ipcRenderer.invoke('electron-slice.control-positions.get'),
  setControlPositions: (positions: Record<string, { x: number; y: number }>): Promise<boolean> =>
    ipcRenderer.invoke('electron-slice.control-positions.set', positions),
})
