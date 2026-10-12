import { extensionProfile } from './extensionProfile.generated.js'
import type { ExtensionManifest } from './extensions.js'

export { extensionProfile }
export function contributionProfile(kind: string, role?: string) {
  return extensionProfile.find(row => row.kind === kind && ('role' in row ? row.role : undefined) === role)
}
export function extensionRequiresRestart(manifest: ExtensionManifest): boolean {
  return Object.entries(manifest.contributes).some(([kind, items]) => items.some(item =>
    contributionProfile(kind, item.role)?.apply !== 'frontend'))
}
export function extensionNeedsAgent(manifest: ExtensionManifest): boolean {
  return Object.entries(manifest.contributes).some(([kind, items]) => items.some(item =>
    (contributionProfile(kind, item.role)?.bind_targets as readonly string[] | undefined)?.includes('agents')))
}
export function matchesExtensionSelection(manifest: ExtensionManifest, selection: {kind: string; role?: string} | undefined, value: string): boolean {
  if (!selection || contributionProfile(selection.kind, selection.role)?.status !== 'open') return false
  return (manifest.contributes[selection.kind] || []).some(item => item.role === selection.role && value === `${manifest.id}/${item.id}`)
}
