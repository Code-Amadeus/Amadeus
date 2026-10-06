import { useI18n } from '../i18n'

export const RETIRED_ROUTE_KEY = 'COOPERATIVE_CHAT_ENABLED'

export interface RetiredSettingFact {
  key: string
  value: boolean
  source: string
  effective_behavior: string
}

interface DesktopSnapshot {
  values?: Record<string, string>
  retired_settings?: RetiredSettingFact[]
}

export function retiredRouteMigration(
  desktop: DesktopSnapshot | null,
  backendFacts: unknown,
): { storedFalse: boolean; externalFalse: RetiredSettingFact[] } {
  const stored = desktop?.values?.[RETIRED_ROUTE_KEY]
  // Read the saved value itself: an environment override must not hide a
  // stored false or cause an unrelated save to acknowledge its migration.
  const storedFalse = stored !== undefined && !['1', 'true', 'yes'].includes(stored.trim().toLowerCase())
  const facts = desktop?.retired_settings ?? (Array.isArray(backendFacts) ? backendFacts : [])
  const externalFalse = facts.filter((fact): fact is RetiredSettingFact =>
    fact?.key === RETIRED_ROUTE_KEY && fact.value === false
      && ['environment', 'dotenv'].includes(fact.source),
  )
  return { storedFalse, externalFalse }
}

export async function removeStoredRetiredRouteSetting<T extends DesktopSnapshot>(
  update: (request: { values: Record<string, null> }) => Promise<{ ok: boolean; settings?: T; error?: string }>,
): Promise<T> {
  const result = await update({ values: { [RETIRED_ROUTE_KEY]: null } })
  if (!result.ok) throw new Error(result.error || 'Could not confirm the retired setting migration')
  if (!result.settings || result.settings.values?.[RETIRED_ROUTE_KEY] !== undefined) {
    throw new Error('The retired setting removal was not confirmed by desktop persistence')
  }
  return result.settings
}

export default function RetiredRouteSetting({
  migration, saving, onConfirm,
}: {
  migration: ReturnType<typeof retiredRouteMigration>
  saving: boolean
  onConfirm: () => Promise<void>
}) {
  const { t } = useI18n()
  if (!migration.storedFalse && migration.externalFalse.length === 0) return null
  return (
    <div role="note" className="text-[11px] rounded-md p-3 mb-4" style={{ color: 'var(--warning)', background: 'var(--warning-bg)' }}>
      <strong>{t('Retired Chat runtime setting')}</strong>
      <p>{t('The old Off value selected the original Chat runtime. It never prohibited Work execution. Cooperative is now the sole Chat runtime; existing Work permissions still apply.')}</p>
      {migration.storedFalse ? <>
        <p>{t('Your saved Off value is retained until you confirm this explanation. Confirmation removes only the retired setting from desktop storage.')}</p>
        <button type="button" disabled={saving} onClick={() => void onConfirm()} className="rounded-md px-3 py-1 disabled:opacity-50" style={{ border: '1px solid var(--warning)', background: 'transparent' }}>
          {t(saving ? 'Saving…' : 'I understand; remove the saved setting')}
        </button>
      </> : null}
      {migration.externalFalse.map(fact => <p key={fact.source}>
        {t(fact.source === 'dotenv'
          ? 'The retired Off value in .env is ignored. This source is read-only in desktop Settings.'
          : 'The retired Off value in the startup environment is ignored. This source is read-only in desktop Settings.')}
      </p>)}
    </div>
  )
}
