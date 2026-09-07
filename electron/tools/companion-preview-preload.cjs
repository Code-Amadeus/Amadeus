const { contextBridge, ipcRenderer } = require('electron');
// Preview owns isolated userData. Do not change the real user's voice preference.
window.localStorage.setItem('amadeus.companion.reminderVoice', 'off');
contextBridge.exposeInMainWorld('amadeus', {
  getBackendConnection: () => ipcRenderer.invoke('preview.connection'),
  focusMainWindow: () => ipcRenderer.invoke('preview.explain'),
  openCodexThread: threadId => ipcRenderer.invoke('preview.codex', threadId),
  closeFloatingCompanion: () => ipcRenderer.invoke('preview.close'),
  setFloatingCompanionHitRegions: regions => ipcRenderer.invoke('preview.hit', regions),
});
