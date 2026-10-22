import { useEffect, useRef, useState } from 'react'
import type { CatalogEntry, CatalogView } from '../../shared/extensionCatalog.js'
import type { ExtensionSummary } from '../../shared/extensions.js'
import { useI18n } from '../i18n'
import { CardShell, SettingsButton, StatusPill } from './SettingsPrimitives'
import { catalogApplication, catalogBytes, catalogContributions, catalogGroups, catalogText, catalogUnavailable, contributionLabel, compareCatalogVersions } from './extensionCatalogModel'

export default function ExtensionCatalog({ catalog, installed, busy, selection, onSelect, onInstall, onLink }: {
  catalog: CatalogView; installed: ExtensionSummary[]; busy: boolean
  selection: { id: string; version: string } | null
  onSelect: (selection: { id: string; version: string } | null) => void
  onInstall: (entry: CatalogEntry) => void
  onLink: (entry: CatalogEntry, field: 'homepage' | 'repository') => void
}) {
  const { locale, t } = useI18n()
  const [query, setQuery] = useState(''), [kind, setKind] = useState('')
  const groups = catalogGroups(catalog.entries, query, kind, locale, t)
  const kinds = [...new Set(catalog.entries.flatMap(entry => catalogContributions(entry).map(c => c.kind)))]
  const selected = selection && catalog.entries.find(e => e.id === selection.id && e.version === selection.version)
  const detail = useRef<HTMLElement>(null)
  useEffect(() => {
    if (selected) { detail.current?.scrollIntoView({ block: 'nearest' }); detail.current?.focus({ preventScroll: true }) }
  }, [selected?.id])
  const prior = selected ? installed.find(item => item.id === selected.id) : undefined
  const unavailable = selected ? catalogUnavailable(selected, prior) : []
  const value = (input: unknown) => typeof input === 'string' ? input : Array.isArray(input) ? input.filter(i => typeof i === 'string').join(', ') : ''
  return <div className="extension-catalog settings-embedded-stack">
    <div className="extension-catalog-filters">
      <label>{t('Search plugins')}<input className="settings-form-input" type="search" value={query} placeholder={t('Name, description or capability')} onChange={event => setQuery(event.target.value)} /></label>
      <label>{t('Plugin type')}<select className="settings-form-input" value={kind} onChange={event => setKind(event.target.value)}><option value="">{t('All types')}</option>{kinds.map(type => <option key={type} value={type}>{t(contributionLabel(type))}</option>)}</select></label>
    </div>
    {catalog.cached && catalog.entries.length > 0 ? <p className="settings-field-description">{t('Showing the last catalog snapshot. Installation checks the official index again.')}</p> : null}
    {!catalog.entries.length ? <p role="status" className="settings-field-description">{t(catalog.error ? 'The catalog is unavailable. Installed packages remain manageable.' : 'Plugin store preview: no packages have been listed yet.')}</p> : !groups.length ? <p role="status">{t('No plugins match this search.')}</p> : null}
    {selected ? <section ref={detail} tabIndex={-1} className="extension-catalog-detail" aria-label={t('Plugin details')}>
      <CardShell vertical>
        <div className="extension-card-heading"><div><h4 className="settings-card-title">{catalogText(selected.display.display_name, locale, selected.id)}</h4><div className="extension-package-identity">{selected.id}</div></div><SettingsButton tone="quiet" onClick={() => onSelect(null)}>{t('Close details')}</SettingsButton></div>
        <p className="extension-catalog-copy">{catalogText(selected.display.description, locale)}</p>
        <label>{t('Catalog version')}<select className="settings-form-input" value={selected.version} onChange={event => onSelect({ id: selected.id, version: event.target.value })}>{catalog.entries.filter(e => e.id === selected.id).sort((a,b) => (compareCatalogVersions(b.version, a.version) || 0)).map(e => <option key={e.version} value={e.version}>{e.version}{e.incompatible.length || e.blockedReason || e.withdrawal ? ` · ${t('Unavailable')}` : ''}</option>)}</select></label>
        <dl className="extension-catalog-facts">
          <dt>{t('Author')}</dt><dd>{catalogText(selected.display.author && typeof selected.display.author === 'object' ? (selected.display.author as Record<string, unknown>).name : selected.display.author, locale, t('Not provided'))}</dd>
          <dt>{t('Maintainers')}</dt><dd>{value(selected.publisher.maintainers) || value(selected.publisher.id)}</dd>
          <dt>{t('License')}</dt><dd>{value(selected.display.license) || t('Not provided')}</dd>
          <dt>{t('Capabilities')}</dt><dd>{catalogContributions(selected).map((c, index) => <div key={`${c.kind}/${c.role}/${c.contract}/${index}`}>{t(contributionLabel(c.kind, c.role))} · {c.contract}</div>)}</dd>
          <dt>{t('Host API')}</dt><dd>{value(selected.compatibility.host_api)}</dd>
          <dt>{t('Platform')}</dt><dd>{value(selected.compatibility.platforms) || t('No platform restriction declared')}</dd>
          <dt>{t('Plugin runtime')}</dt><dd>{selected.compatibility.runtime === 'none' ? t('No plugin process') : selected.compatibility.runtime === 'python' ? t('Private Python environment') : value(selected.compatibility.runtime)}</dd>
          <dt>{t('Python compatibility')}</dt><dd>{value(selected.compatibility.python) || t('No version restriction declared')} · {t('SDK')} {value(selected.compatibility.sdk)}</dd>
          <dt>{t('Wheel download size')}</dt><dd>{catalogBytes(selected.size)} · {t('Excludes external services and runtimes.')}</dd>
          <dt>{t('When changes apply')}</dt><dd>{t(catalogApplication(selected))}</dd>
          <dt>{t('Artifact URL')}</dt><dd className="extension-package-identity">{selected.url}</dd>
          <dt>{t('SHA-256')}</dt><dd className="extension-package-identity">{selected.sha256}</dd>
          <dt>{t('Artifact review')}</dt><dd>{value(selected.review.reviewer)} · {value(selected.review.date)} · {value(selected.review.policy)}</dd>
        </dl>
        <p className="settings-field-description">{t('Review covers this wheel digest against the public checklist. It does not grant permissions or guarantee runtime safety.')}</p>
        <p className="settings-field-description">{t('The checklist covers wheel contents, dependencies, commands, runtime code downloads and asset licensing. Each version requires a separate review.')}</p>
        {catalog.checkedAt ? <p className="settings-field-description">{t('Catalog checked at')}: {new Date(catalog.checkedAt).toLocaleString(locale)}</p> : <p>{t('Catalog check time is unavailable.')}</p>}
        <details className="settings-field-description"><summary>{t('External services, commands and declared OS access')}</summary>{selected.display.access ? <pre className="extension-catalog-copy">{JSON.stringify(selected.display.access, null, 2)}</pre> : <p>{t('This catalog has no access details. Review the verified wheel disclosure before confirming installation.')}</p>}</details>
        {prior ? <p>{t('Installed version')}: {prior.version} · {prior.digest === selected.sha256 ? t('Exact artifact match') : t('Different artifact')}</p> : null}
        {unavailable.map(reason => <div className="settings-form-error" key={reason}>{t(reason)}</div>)}
        <div className="settings-form-actions-end">
          {typeof selected.display.homepage === 'string' ? <SettingsButton disabled={busy} onClick={() => onLink(selected, 'homepage')}>{t('Open homepage')}</SettingsButton> : null}
          {typeof selected.display.repository === 'string' ? <SettingsButton disabled={busy} onClick={() => onLink(selected, 'repository')}>{t('Open source repository')}</SettingsButton> : null}
          <SettingsButton tone="primary" disabled={busy || unavailable.length > 0} onClick={() => onInstall(selected)}>{t(prior ? 'Install selected version…' : 'Install plugin…')}</SettingsButton>
        </div>
      </CardShell>
    </section> : selection ? <p role="status">{t('This version is no longer in the catalog. Refresh or choose another version.')}</p> : null}
    <div className="extension-catalog-results" aria-label={t('Catalog packages')}>{groups.map(group => {
      const entry = group.versions.find(e => !e.incompatible.length && !e.blockedReason && !e.withdrawal) || group.versions[0]
      return <CardShell vertical key={group.id}>
        <div className="extension-card-heading"><h4 className="settings-card-title">{catalogText(entry.display.display_name, locale, group.id)}</h4><StatusPill ok={false} tone="neutral">{entry.version}</StatusPill></div>
        <div className="settings-card-description extension-catalog-copy">{catalogText(entry.display.description, locale)}</div>
        <div className="settings-field-description">{catalogContributions(entry).map(c => t(contributionLabel(c.kind, c.role))).join(' · ')} · {catalogBytes(entry.size)}</div>
        {catalogUnavailable(entry).map(reason => <div className="settings-form-error" key={reason}>{t(reason)}</div>)}
        <div className="settings-form-actions-end"><SettingsButton onClick={() => onSelect({ id: entry.id, version: entry.version })}>{t('View plugin details')}</SettingsButton></div>
      </CardShell>
    })}</div>
  </div>
}
