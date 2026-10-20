import type { CatalogEntry } from '../../shared/extensionCatalog.js'
import type { ExtensionSummary } from '../../shared/extensions.js'
import { contributionProfile } from '../../shared/extensionProfile.js'

export function catalogText(value: unknown, locale: string, fallback = ''): string {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fallback
  const localized = value as Record<string, unknown>
  return [localized[locale], localized[locale.split('-')[0]], localized.en, ...Object.values(localized)].find(item => typeof item === 'string') as string || fallback
}
export function compareCatalogVersions(a: string, b: string): number | null {
  const pattern = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/
  if (!pattern.test(a) || !pattern.test(b)) return null
  const left = a.split('.').map(BigInt), right = b.split('.').map(BigInt)
  for (let i = 0; i < 3; i++) if (left[i] !== right[i]) return left[i] > right[i] ? 1 : -1
  return 0
}
export function catalogContributions(entry: CatalogEntry): Array<{ kind: string; role: string; contract: string }> {
  const items = Array.isArray(entry.compatibility.contributions) ? entry.compatibility.contributions : []
  return [...new Map(items.map(item => [JSON.stringify([item.kind, item.role, item.contract]), item])).values()]
}
export const contributionLabel = (kind: string) => ({ voice_backend: 'Speech synthesis', mcp_server: 'MCP', skill: 'Skill', theme: 'Theme' }[kind] || kind)
export function catalogApplication(entry: CatalogEntry): string {
  const profiles = catalogContributions(entry).map(c => contributionProfile(c.kind, c.role || undefined))
  return !profiles.length || profiles.some(p => !p || p.status !== 'open') ? 'Compatibility unavailable'
    : profiles.every(p => p?.apply === 'frontend') ? 'Applies when enabled; no backend restart.' : 'Applies after enabling and restarting the backend.'
}
export function catalogUnavailable(entry: CatalogEntry, installed?: ExtensionSummary): string[] {
  return [entry.blockedReason, entry.withdrawal ? entry.withdrawal.reason === 'copyright' ? 'Withdrawn for copyright reasons' : 'Withdrawn by the maintainer' : '',
    ...entry.incompatible, installed?.pendingRemoval ? 'Removal pending restart' : '',
    installed?.version === entry.version ? installed.digest === entry.sha256 ? 'This artifact is already installed' : 'A different artifact of this version is already installed' : ''].filter((value): value is string => Boolean(value))
}
export function recommendedUpdate(entries: CatalogEntry[], installed: ExtensionSummary): CatalogEntry | undefined {
  if (installed.pendingRemoval || compareCatalogVersions(installed.version, installed.version) === null) return undefined
  return entries.filter(entry => entry.id === installed.id && compareCatalogVersions(entry.version, installed.version) === 1 && !catalogUnavailable(entry, installed).length)
    .sort((a, b) => compareCatalogVersions(b.version, a.version) || 0)[0]
}
export function catalogGroups(entries: CatalogEntry[], query: string, kind: string, locale: string, translate: (text: string) => string = text => text): Array<{ id: string; versions: CatalogEntry[] }> {
  const groups = new Map<string, CatalogEntry[]>()
  for (const entry of entries) {
    const versions = groups.get(entry.id)
    if (versions) versions.push(entry)
    else groups.set(entry.id, [entry])
  }
  const term = query.trim().toLocaleLowerCase(locale)
  const matches = (entry: CatalogEntry) => {
    const contributions = catalogContributions(entry)
    return (!kind || contributions.some(c => c.kind === kind)) && (!term || [entry.id, catalogText(entry.display.display_name, locale), catalogText(entry.display.description, locale), ...contributions.flatMap(c => [c.kind, c.role, c.contract, translate(contributionLabel(c.kind))])].join(' ').toLocaleLowerCase(locale).includes(term))
  }
  return [...groups].map(([id, versions]) => ({ id, versions: versions.filter(matches).sort((a, b) => compareCatalogVersions(b.version, a.version) || 0) }))
    .filter(group => group.versions.length > 0)

}
export const catalogBytes = (size: number): string => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(1)} MiB`
