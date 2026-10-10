import { catalogConfiguration, catalogGroups } from '../../shared/configCatalog.js'
import type { StartupSnapshot } from '../../shared/startupSettings.js'
import type { ModelConnectionCatalogGroup } from './modelConnectionCatalog'

export function buildModelRoleCatalog(snapshot?: StartupSnapshot | null): ModelConnectionCatalogGroup[] {
  return catalogGroups.filter(group => group.section === 'roles').sort((a, b) => (a.order || 0) - (b.order || 0))
    .map(group => ({
      ...catalogConfiguration(group.id, snapshot),
      active: group.id === 'vn_companion', configured: true,
      status: 'Backend status unavailable', status_ok: false,
    }))
}
