/** Minimal CommonJS capability boundary for the sandboxed Slice surface. */

import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('amadeus', {
  setElectronSliceShape: (
    bounds: Array<{ x: number; y: number; width: number; height: number }>,
  ): Promise<boolean> => ipcRenderer.invoke('electron-slice.set-shape', bounds),
})
