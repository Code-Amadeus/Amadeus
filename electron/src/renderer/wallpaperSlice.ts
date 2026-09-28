export const ELECTRON_SLICE_START_PARAMS = Object.freeze({ slice_host: 'electron' })

export async function syncElectronSliceHost(payload: Record<string, unknown>): Promise<boolean> {
  if (String(payload.sliceHost || '') !== 'electron') return false
  const assetPort = Number(payload.assetPort)
  const bridgePort = Number(payload.bridgePort)
  if (!Number.isInteger(assetPort) || !Number.isInteger(bridgePort)) return false
  return window.amadeus?.openElectronSlice({
    assetPort,
    bridgePort,
    assetVersion: String(payload.assetVersion || ''),
    graphicsProfile: String(payload.graphicsProfile || 'standard'),
    renderMaxFps: Number(payload.renderMaxFps),
    renderTextureSampling: payload.renderTextureSampling === true,
    renderMaxResolution: payload.renderMaxResolution == null
      ? null
      : Number(payload.renderMaxResolution),
    sliceBounds: payload.sliceBounds && typeof payload.sliceBounds === 'object'
      ? payload.sliceBounds as { x: number; y: number; width: number; height: number }
      : undefined,
  }) ?? false
}


// Both the wallpaper toggle and switching to Render end the same session.
// A failed backend RPC must reach the desktop owner before hiding the mode.
export async function stopElectronSliceHost(
  send: (method: string, params: Record<string, unknown>) => Promise<Record<string, unknown>>,
): Promise<boolean> {
  let backendStopError: string | undefined
  try { await send('wallpaper.stop', {}) }
  catch (error) { backendStopError = String(error) }
  if (!window.amadeus) return backendStopError === undefined
  try { return await window.amadeus.closeElectronSlice(backendStopError) }
  catch (error) {
    console.error('[wallpaper] desktop stop failed:', backendStopError, error)
    return false
  }
}
