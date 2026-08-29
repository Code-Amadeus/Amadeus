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
    sliceBounds: payload.sliceBounds && typeof payload.sliceBounds === 'object'
      ? payload.sliceBounds as { x: number; y: number; width: number; height: number }
      : undefined,
  }) ?? false
}
