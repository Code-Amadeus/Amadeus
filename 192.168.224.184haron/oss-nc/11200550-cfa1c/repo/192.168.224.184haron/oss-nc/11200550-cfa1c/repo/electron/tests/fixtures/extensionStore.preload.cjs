const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('extensionStoreFixture', {
  extensionCatalog: request => ipcRenderer.invoke('store.catalog', request),
  getDesktopSettings: () => ipcRenderer.invoke('store.settings'),
  updateDesktopSettings: update => ipcRenderer.invoke('store.settings.update', update),
  updateExtension: update => ipcRenderer.invoke('store.update', update),
  removeExtension: (id, options) => ipcRenderer.invoke('store.remove', id, options),
  getDesktopTheme: () => ipcRenderer.invoke('store.theme'),
  onDesktopThemeChanged: callback => { const listener = () => callback(); ipcRenderer.on('store.theme.changed', listener); return () => ipcRenderer.removeListener('store.theme.changed', listener) },
})
