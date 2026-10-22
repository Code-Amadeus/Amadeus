import type { CatalogEntry, CatalogReply, CatalogView, CatalogProgress } from '../../shared/extensionCatalog.js'
import ExtensionCatalog from './ExtensionCatalog'
import { catalogBytes, recommendedUpdate } from './extensionCatalogModel'
import { useEffect, useRef, useState } from 'react'
import type { ExtensionSummary, ExtensionMutationResult } from '../../shared/extensions.js'
import { extensionDisclosure } from '../../shared/extensions.js'
import { extensionNeedsAgent } from '../../shared/extensionProfile.js'
import { useI18n } from '../i18n'
import { CardShell, SettingsButton, SettingsField, SettingsGroup, StatusPill } from './SettingsPrimitives'
import Switch from './Switch'
import ExtensionIcon from './ExtensionIcon'
import { canLeaveSettingsEditors, DISCARD_CONNECTION_CHANGES, type SettingsEditorState } from './settingsDraft'
import { compatibleExtensionProviders, extensionDraft, extensionLanguageSupported, extensionRuntimeLabel, extensionText, extensionUpdate,
  type ExtensionDraft, type ExtensionProvider, type ExtensionRuntimeStatus } from './extensionSettingsCatalog'

const ATTENTION_STATES = new Set(['error', 'degraded', 'needs_config', 'incompatible'])

interface Props {
  extensions: ExtensionSummary[]
  runtime: ExtensionRuntimeStatus[]
  providers: ExtensionProvider[]
  connected: boolean
  available: boolean
  encryptionAvailable: boolean
  selectedVoiceBackend: string
  selectedTheme?: string
  appliedThemeVersion?: string
  onOpenAppearance?: () => void
  onSettingsChanged: (settings: Record<string, unknown>) => void
  onRestartRequired: () => void
  onOpenVoice: () => void
  onEditorStateChange?: (state: SettingsEditorState) => void
}

export default function ExtensionSettings({ extensions, runtime, providers, connected, available, encryptionAvailable, selectedVoiceBackend, selectedTheme, appliedThemeVersion, onOpenAppearance,
  onSettingsChanged, onRestartRequired, onOpenVoice, onEditorStateChange }: Props) {
  const { locale, t } = useI18n()
  const [selected, setSelected] = useState<ExtensionSummary | null>(null)
  const [draft, setDraft] = useState<ExtensionDraft | null>(null)
  const initial = useRef('')
  const editor = useRef<HTMLFormElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [security, setSecurity] = useState<{ status: string; checkedAt?: string }>({ status: 'unseen' })
  const [tab, setTab] = useState<'installed' | 'browse'>('installed')
  const [catalog, setCatalog] = useState<CatalogView>({ entries: [], cached: true })
  const [catalogSelection, setCatalogSelection] = useState<{ id: string; version: string } | null>(null)
  const [progress, setProgress] = useState<CatalogProgress | undefined>()
  const catalogBusy = useRef(false)
  const metadataRequest = useRef(0)
  const receiveCatalog = (result: CatalogReply) => {
    if (result.catalog) setCatalog(result.catalog)
    if (result.settings) {
      onSettingsChanged(result.settings)
      if (result.settings.extensionSecurity) setSecurity(result.settings.extensionSecurity as { status: string; checkedAt?: string })
    }
  }
  useEffect(() => {
    let closed = false
    const request = ++metadataRequest.current
    void window.amadeus?.extensionCatalog?.({ action: 'get' }).then(result => {
      if (!closed && request === metadataRequest.current) { receiveCatalog(result); setProgress(result.catalog?.downloading) }
    }).catch(() => { if (!closed) setError('Catalog state could not be loaded.') })
    return () => { closed = true }
  }, [])
  useEffect(() => {
    const api = window.amadeus
    if (!api || !busy && !progress) return
    let closed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const result = await api.extensionCatalog({ action: 'status' })
        if (!closed) {
          setProgress(result.progress)
          if (!result.progress && !catalogBusy.current && progress) { const snapshot = await api.extensionCatalog({ action: 'get' }); if (!closed) receiveCatalog(snapshot) }
        }
      } catch { /* The operation receipt owns errors; polling cannot declare success. */ }
      if (!closed) timer = setTimeout(poll, 350)
    }
    void poll()
    return () => { closed = true; clearTimeout(timer) }
  }, [busy, Boolean(progress)])
  const [removeCandidate, setRemoveCandidate] = useState('')
  const [disableCandidate, setDisableCandidate] = useState('')
  const [deleteData, setDeleteData] = useState(false)
  const dirty = draft !== null && JSON.stringify(draft) !== initial.current
  useEffect(() => {
    onEditorStateChange?.({ dirty, busy: busy || Boolean(progress) })
    return () => onEditorStateChange?.({ dirty: false, busy: false })
  }, [dirty, busy, Boolean(progress), onEditorStateChange])
  useEffect(() => {
    if (!selected) return
    editor.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    editor.current?.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true })
  }, [selected])
  const canLeave = () => canLeaveSettingsEditors([{ dirty, busy: busy || Boolean(progress) }], () => window.confirm(t(DISCARD_CONNECTION_CHANGES)))
  const close = () => { setSelected(null); setDraft(null); setRemoveCandidate(''); setDisableCandidate(''); setDeleteData(false) }
  const edit = (extension: ExtensionSummary) => {
    if (!extension.manifest || !canLeave()) return
    const next = extensionDraft(extension)
    initial.current = JSON.stringify(next)
    // Capture the saved baseline. Runtime and settings refreshes cannot erase a draft.
    setSelected(extension)
    setDraft(next)
    setRemoveCandidate('')
    setDisableCandidate('')
    setDeleteData(false)
    setError('')
    setNotice('')
  }
  const change = (update: Partial<ExtensionDraft>) => setDraft(current => current ? { ...current, ...update } : current)
  const complete = (result: ExtensionMutationResult, failure: string) => {
    if (result.cancelled) return false
    if (!result.ok) throw new Error(result.error || failure)
    if (result.settings) onSettingsChanged(result.settings)
    if (result.settings?.restartRequired) onRestartRequired()
    // This is an operation receipt. Package cards own the changing applied state.
    setNotice('Saved. Current application status is shown on each package.')
    return true
  }
  const install = async () => {
    if (!window.amadeus || !canLeave()) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (complete(await window.amadeus.installExtension(), 'Could not install extension')) close()
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('Could not install extension')) }
    finally { setBusy(false) }
  }
  const checkSecurity = async (recovery = false) => {
    if (!window.amadeus || busy || catalogBusy.current) return
    catalogBusy.current = true
    ++metadataRequest.current
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await window.amadeus.extensionCatalog({ action: recovery ? 'recover' : 'refresh' })
      receiveCatalog(result)
      if (!result.ok) throw new Error(result.error || 'Catalog refresh failed')
      setNotice('Official extension security state checked. Existing backend processes may still need to stop.')
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Catalog refresh failed') }
    finally { catalogBusy.current = false; setBusy(false) }
  }
  const installCatalog = async (entry: CatalogEntry) => {
    if (!window.amadeus || catalogBusy.current || !canLeave()) return
    catalogBusy.current = true
    ++metadataRequest.current
    setBusy(true); setError(''); setNotice('')
    setProgress({ id: entry.id, version: entry.version, received: 0, total: 0, stage: 'checking' })
    try {
      const result = await window.amadeus.extensionCatalog({ action: 'install', id: entry.id, version: entry.version })
      receiveCatalog(result)
      if (result.cancelled) { setNotice('Catalog installation cancelled. No new package was saved.'); return }
      if (!result.ok) throw new Error(result.error || 'Could not install extension')
      close(); setTab('installed')
      setNotice('Package saved. Configure and enable it in Installed, then choose its Agents, voice or appearance. Saved updates may require a backend restart.')
      if (result.settings?.restartRequired) onRestartRequired()
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not install extension') }
    finally { catalogBusy.current = false; setProgress(undefined); setBusy(false) }
  }
  const cancelCatalog = async () => {
    if (!window.amadeus) return
    setProgress(current => current ? { ...current, cancelRequested: true } : current)
    try { const result = await window.amadeus.extensionCatalog({ action: 'cancel' }); if (!result.ok) throw new Error(result.error) }
    catch { setError('Cancellation could not be requested. The operation is still running.') }
  }
  const openCatalogLink = async (entry: CatalogEntry, action: 'homepage' | 'repository') => {
    if (!window.amadeus) return
    setError(''); setNotice('')
    try { const result = await window.amadeus.extensionCatalog({ action, id: entry.id, version: entry.version }); if (!result.ok) throw new Error(result.error) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not open package link') }
  }
  const showCatalog = (id: string, version: string) => { setCatalogSelection({ id, version }); setTab('browse') }
  const switchTab = (next: 'installed' | 'browse') => {
    setTab(next)
    if (next === 'browse' && tab !== next && security.status !== 'unavailable') void checkSecurity()
  }
  const save = async () => {
    if (!window.amadeus || !selected || !draft) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (complete(await window.amadeus.updateExtension(extensionUpdate(selected, draft)), 'Could not save extension')) close()
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('Could not save extension')) }
    finally { setBusy(false) }
  }
  // The card switch changes only enabled; values, secrets and Agent bindings stay as saved.
  const setEnabled = async (extension: ExtensionSummary, enabled: boolean) => {
    if (!window.amadeus) return
    setDisableCandidate('')
    setBusy(true)
    setError('')
    setNotice('')
    try {
      complete(await window.amadeus.updateExtension(extensionUpdate(extension, extensionDraft(extension), enabled)), 'Could not save extension')
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('Could not save extension')) }
    finally { setBusy(false) }
  }
  const remove = async (id: string, deleteSavedData = deleteData) => {
    if (!window.amadeus || !canLeave()) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (complete(await window.amadeus.removeExtension(id, { deleteData: deleteSavedData }), 'Could not remove extension')) close()
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('Could not remove extension')) }
    finally { setBusy(false) }
  }
  const compatibleProviders = compatibleExtensionProviders(providers, selected)
  const selectedMetadataAvailable = selected && extensions.some(extension => extension.id === selected.id && extension.manifest)
  const selectedPolicyBlocked = security.status === 'unavailable' || Boolean(catalog.installed?.find(item => item.id === selected?.id && item.version === selected?.version && item.digest === selected?.digest)?.blocked)
  const missingProviders = draft?.providers.filter(id => !compatibleProviders.some(provider => provider.provider_id === id)) || []

  const editorForm = (current: ExtensionSummary) => !selected?.manifest || !draft ? null : <form ref={editor} className="extension-card-editor settings-form" onSubmit={event => { event.preventDefault(); void save() }} aria-label={t('Configure extension')}>
      {selectedPolicyBlocked && current.enabled ? <div role="alert" className="settings-form-error">{t('Security policy prevents activation. Disable this package to save configuration changes.')}</div> : null}
      <fieldset className="settings-form-body" disabled={busy}>
        {Object.entries(selected.manifest.config || {}).map(([key, field]) => {
          const id = `extension-config-${key}`
          const label = extensionText(field.title, locale, key)
          const description = extensionText(field.description, locale)
          const secret = field.secret === true
          return <SettingsField key={key} id={id} label={label} description={description}>
            {secret ? <div className="extension-secret-field">
              <div className="settings-field-description">{t(draft.secrets[key] === null ? 'Will be cleared after saving' : selected.secretConfigured[key] ? 'Configured' : 'Not configured')}</div>
              <input id={id} className="settings-form-input" type="password" autoComplete="new-password" value={draft.secrets[key] || ''}
                aria-describedby={description ? `${id}-hint` : undefined} maxLength={field.max_length} disabled={!encryptionAvailable}
                placeholder={t(selected.secretConfigured[key] ? 'Configured — enter to replace' : 'Not configured')}
                required={current.enabled && field.required && !selected.secretConfigured[key]}
                onChange={event => change({ secrets: { ...draft.secrets, [key]: event.target.value } })} />
              {selected.secretConfigured[key] || draft.secrets[key] ? <SettingsButton tone="danger" disabled={!encryptionAvailable} onClick={() => change({ secrets: { ...draft.secrets, [key]: null } })}>{t('Clear')} {label}</SettingsButton> : null}
              {!encryptionAvailable ? <div className="settings-field-description">{t('Secure credential storage is unavailable. Credentials cannot be saved on this device.')}</div> : null}
            </div> : field.type === 'boolean' ? <Switch id={id} describedBy={description ? `${id}-hint` : undefined} checked={draft.values[key] === true}
                onChange={value => change({ values: { ...draft.values, [key]: value } })} />
              : field.type === 'enum' ? <select id={id} className="settings-form-input" value={String(draft.values[key] ?? '')} required={current.enabled && field.required}
                aria-describedby={description ? `${id}-hint` : undefined} onChange={event => change({ values: { ...draft.values, [key]: event.target.value } })}>
                <option value="">{t('Not set')}</option>
                {(field.options || []).map(option => { const value = typeof option === 'string' ? option : option.value
                  return <option value={value} key={value}>{typeof option === 'string' ? option : extensionText(option.label, locale, value)}</option> })}
              </select> : <input id={id} className="settings-form-input" type={field.type === 'url' ? 'url' : field.type === 'integer' || field.type === 'number' ? 'number' : 'text'}
                value={String(draft.values[key] ?? '')} min={field.min} max={field.max} step={field.type === 'integer' ? 1 : 'any'} maxLength={field.max_length}
                required={current.enabled && field.required} aria-describedby={description ? `${id}-hint` : undefined}
                onChange={event => change({ values: { ...draft.values, [key]: event.target.value } })} />}
          </SettingsField>
        })}
        {extensionNeedsAgent(selected.manifest) ? <section className="settings-form-section">
          <h4 className="settings-card-title">{t('Compatible Work Providers')}</h4>
          <p className="settings-field-description">{t('Select the Agents that may use this package. Changes apply after restarting the backend. Skill updates or removal require a new Agent session; existing context cannot be erased by deleting files.')}</p>
          <div className="settings-form-provider-choices">{compatibleProviders.map(provider => <label key={provider.provider_id}>
            <input type="checkbox" checked={draft.providers.includes(provider.provider_id)} onChange={event => change({ providers: event.target.checked ? [...draft.providers, provider.provider_id] : draft.providers.filter(id => id !== provider.provider_id) })} />{provider.display_name}
          </label>)}</div>
          {!compatibleProviders.length ? <p className="settings-field-description">{t(connected ? 'No compatible Work Provider is registered.' : 'Connect the backend to inspect compatible Work Providers. Saved bindings are retained.')}</p> : null}
          {missingProviders.map(id => <div key={id} className="extension-unavailable-binding"><span>{id} · {t('Compatibility unavailable')}</span><SettingsButton tone="quiet" onClick={() => change({ providers: draft.providers.filter(value => value !== id) })}>{t('Remove binding')}</SettingsButton></div>)}
        </section> : null}
      </fieldset>
      <div className="settings-form-footer"><div className="settings-form-save-hint">{t(selected.application === 'frontend' ? 'Save to apply immediately without restarting the backend.' : 'Save, then restart the backend to apply. The current runtime keeps its existing extension state.')}</div>
        <div className="settings-form-actions-end"><SettingsButton tone="quiet" disabled={busy} onClick={() => { if (canLeave()) close() }}>{t('Cancel')}</SettingsButton>
          <button type="submit" className="settings-button" data-tone="primary" disabled={busy || !dirty || selectedPolicyBlocked && current.enabled}>{t(busy ? 'Saving…' : 'Save extension')}</button></div>
      </div>
    </form>

  return <div className="settings-embedded-stack extension-settings">
    <div role="tablist" aria-label={t('Plugin manager views')} className="capability-page-tabs">
      {(['installed', 'browse'] as const).map(value => <button type="button" key={value} role="tab" id={`extension-tab-${value}`} aria-controls={`extension-panel-${value}`} aria-selected={tab === value} tabIndex={tab === value ? 0 : -1}
        onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 'installed' : event.key === 'End' ? 'browse' : tab === 'installed' ? 'browse' : 'installed'; switchTab(next); document.getElementById(`extension-tab-${next}`)?.focus() } }}
        onClick={() => switchTab(value)}>{t(value === 'installed' ? 'Installed' : 'Browse')}</button>)}
    </div>
    <div className="extension-catalog-toolbar">
      <SettingsButton disabled={!available || busy} onClick={() => void checkSecurity(security.status === 'unavailable')}>{t(security.status === 'unavailable' ? 'Recover official extension security state' : 'Check updates and security status')}</SettingsButton>
      <div className="settings-field-description">{security.status === 'unavailable' ? t('Extension security state is damaged. Installation and activation are blocked until recovery.') : security.checkedAt ? `${t('Last successful security check')}: ${new Date(security.checkedAt).toLocaleString(locale)}` : t('Official extension security state has not been checked.')}</div>
    </div>
    {error || catalog.error ? <div role="alert" className="settings-form-error">{t(error || catalog.error!)}</div> : null}
    {notice ? <div role="status" className="settings-form-save-hint">{t(notice)}</div> : null}
    {progress ? <div className="extension-download-progress" role="status" aria-live="polite">
      <strong>{progress.id} · {progress.version}</strong>
      <div>{t(progress.cancelRequested ? 'Cancellation requested; waiting for safe cleanup.' : { checking: 'Checking the official catalog…', downloading: 'Downloading wheel…', verifying: 'Downloaded. Verifying the artifact…', confirming: 'Review the native installation confirmation.', preparing: 'Preparing the package. It has not been saved yet.' }[progress.stage])}</div>
      {progress.stage === 'downloading' ? <><progress aria-label={t('Wheel download progress')} max={progress.total || 1} value={progress.received} /><span>{catalogBytes(progress.received)} / {catalogBytes(progress.total)}</span></> : null}
      <SettingsButton disabled={progress.cancelRequested} onClick={() => void cancelCatalog()}>{t('Cancel installation')}</SettingsButton>
    </div> : null}
    <section id="extension-panel-installed" role="tabpanel" aria-labelledby="extension-tab-installed" hidden={tab !== 'installed'} className="settings-embedded-stack">
    <SettingsGroup title="Installed plugins"
    action={<SettingsButton tone="primary" disabled={!available || busy || security.status === 'unavailable'} onClick={() => void install()}>{t(busy && !selected ? 'Working…' : 'Install plugin…')}</SettingsButton>}>
    <div className="settings-embedded-stack">
    <details className="extension-install-help">
      <summary>{t('About plugin installation')}</summary>
      <div className="settings-note-description">{t('Install a local wheel from an author you trust. Frontend-only packages apply immediately. Packages with backend contributions apply together after backend restart.')}</div>
      <div className="settings-note-description">{t('This pilot supports themes, TTS, MCP servers and Agent Skills. Install once, then choose a theme, voice or compatible Agents. Main Chat has no tool or Skill access.')}</div>
    </details>
    {!available ? <div className="settings-field-description">{t('Extension management is available in the Electron app.')}</div> : null}


    {available && !extensions.length ? <div className="settings-field-description">{t('No community extensions installed.')}</div> : null}
    {extensions.map(extension => {
      if (!extension.manifest) return <CardShell vertical key={extension.id}>
        <div className="extension-card-heading"><div className="extension-card-identity" data-extension-id={extension.id} data-extension-version={extension.version}><ExtensionIcon id={extension.id} name={extension.id} /><div className="settings-card-title extension-package-identity">{extension.id} · {extension.version}</div></div>
          <StatusPill ok={false}>{t(extension.status === 'incompatible' ? 'Incompatible' : 'Extension unavailable')}</StatusPill></div>
        <div role="alert" className="settings-field-description">{t(extension.validationError || 'Extension metadata is unavailable.')}</div>
        {!extension.pendingRemoval && <div className="settings-form-actions-end">
          <SettingsButton tone="danger" disabled={!available || busy} onClick={() => {
            if (canLeave() && window.confirm(t('Remove extension? Saved data will be retained.'))) void remove(extension.id, false)
          }}>{t('Remove')}</SettingsButton>
        </div>}
      </CardShell>
      const listing = catalog.installed?.find(item => item.id === extension.id && item.version === extension.version && item.digest === extension.digest)
      const update = recommendedUpdate(catalog.entries, extension)
      const observed = connected ? runtime.find(item => item.id === extension.id) : undefined
      const voices = extension.manifest.contributes.voice_backend || []
      const themes = extension.manifest.contributes.theme || []
      const frontend = extension.application === 'frontend'
      const nativeContributions = [
        ...(extension.manifest.contributes.mcp_server || []).map(item => ({ ...item, kind: 'mcp_server', label: 'MCP' })),
        ...(extension.manifest.contributes.skill || []).map(item => ({ ...item, kind: 'skill', label: 'Skill' })),
      ]
      const selectedVoice = voices.some(voice => `${extension.id}/${voice.id}` === selectedVoiceBackend)
      const savedState = extension.pendingRemoval ? (frontend ? 'Cleanup pending' : 'Removal pending restart') : extension.enabled ? (frontend ? 'Enabled' : 'Enabled on startup') : 'Disabled'
      const name = extensionText(extension.manifest.display_name, locale, extension.id)
      const editing = selected?.id === extension.id && draft !== null
      const pending = extension.restartRequired || extension.pendingRemoval
      const currentState = frontend
        ? extension.applicationError ? 'Unavailable' : extension.enabled && extension.appliedRevision === extension.revision ? 'Applied' : 'Disabled'
        : extensionRuntimeLabel(observed?.state)
      const needsAttention = Boolean(observed && ATTENTION_STATES.has(observed.state))
      const currentTone = extension.applicationError || needsAttention ? 'warning'
        : currentState === 'Applied' || observed?.state === 'active' || observed?.state === 'ready' ? 'success' : 'neutral'
      return <CardShell vertical key={extension.id}>
        <div className="extension-card-heading">
          <div className="extension-card-identity" data-extension-id={extension.id} data-extension-version={extension.version}>
            <ExtensionIcon key={extension.digest} id={extension.id} name={name} icon={extension.manifest.icon} />
            <div className="min-w-0"><h4 className="settings-card-title">{name}</h4>
              <div className="settings-card-description extension-package-identity">{extension.manifest.author.name} · {extension.version}</div></div>
          </div>
          <div className="extension-card-controls">
            <StatusPill ok={false} tone={pending ? 'warning' : currentTone}>{t(pending ? extension.restartRequired ? 'Restart required' : savedState : currentState)}</StatusPill>
            <Switch id={`extension-enabled-${extension.id}`} label={t('Enable {name}', { name })} stateText={false} checked={extension.enabled}
              disabled={!available || busy || Boolean(progress) || extension.pendingRemoval || !extension.enabled && (security.status === 'unavailable' || Boolean(listing?.blocked))}
              onChange={enabled => { if (!enabled && selectedVoice) setDisableCandidate(extension.id); else void setEnabled(extension, enabled) }} />
          </div>
        </div>
        <div className="settings-card-description extension-card-description">{extensionText(extension.manifest.description, locale)}</div>
        <div className="settings-field-description">{t(listing?.catalogState === 'matched' ? catalog.cached ? 'Reviewed artifact in the cached catalog' : 'Reviewed artifact in the checked catalog' : listing?.catalogState === 'unlisted' ? 'Not listed in the checked catalog' : 'Catalog review status is unknown')}{listing?.withdrawal ? ` · ${t(listing.withdrawal.reason === 'copyright' ? 'Withdrawn for copyright reasons' : 'Withdrawn by the maintainer')}` : ''}{listing?.blocked ? ` · ${t('Blocked by security policy')}` : ''}</div>
        {update ? <div className="extension-catalog-update"><span>{t('Compatible update available')}: {extension.version} → {update.version}</span><SettingsButton disabled={busy || Boolean(progress)} onClick={() => void installCatalog(update)}>{t('Update plugin…')}</SettingsButton><SettingsButton tone="quiet" onClick={() => showCatalog(update.id, update.version)}>{t('Review update details')}</SettingsButton></div> : null}

        {pending ? <div className="extension-status-note" role="status">
          <span>{t('Saved state')}: {t(savedState)}</span>
          <span>{t(frontend ? 'Current appearance' : 'Current backend')}: {t(currentState)}</span>
        </div> : null}
        {extension.frontendSuppressed && extension.backendStillRunning ? <div role="status" className="settings-field-description">{t('Frontend access is closed; the previous backend still runs until restart.')}</div> : null}
        {extension.applicationError ? <div role="alert" className="settings-form-error">{extension.applicationError}</div> : null}
        {needsAttention && observed?.detail ? <div role="status" className="extension-attention">{t(observed.detail)}</div> : null}
        {observed?.contributions.filter(item => ATTENTION_STATES.has(item.health)).map(item => (
          <div key={`${item.kind}-${item.id}`} role="status" className="extension-attention">
            {extensionText(extension.manifest!.contributes[item.kind]?.find(contribution => contribution.id === item.id)?.title, locale, item.id)} · {t(extensionRuntimeLabel(item.health))}{item.detail ? ` · ${t(item.detail)}` : ''}
          </div>
        ))}
        <ul className="extension-contributions" aria-label={t('Contributions')}>
          {themes.map(theme => <li key={`theme-${theme.id}`}>
            <div className="extension-contribution-heading"><strong>{t('Theme')}{theme.title ? ` · ${extensionText(theme.title, locale)}` : ''}</strong>
              <span>{t(theme.base === 'classic' ? 'Classic light' : 'Wallpaper slice')}</span>
              {selectedTheme === `${extension.id}/${theme.id}` ? <span className="settings-tag" data-tone="accent">{t('Saved theme')}</span> : null}
            </div>
          </li>)}
          {voices.map(voice => <li key={`voice-${voice.id}`}>
            <div className="extension-contribution-heading"><strong>{t('Speech synthesis')}{voice.title ? ` · ${extensionText(voice.title, locale)}` : ''}</strong>
              <span>{t(voice.deployment === 'remote' ? 'Remote speech service' : 'Local speech engine')} · {(voice.languages || []).join(', ')}</span>
            </div>
            {!extensionLanguageSupported(voice.languages || [], 'ja') ? <div className="extension-compatibility">{t('Japanese support is not declared. Select English for this voice; Japanese output has not been verified.')}</div> : null}
            {!extensionLanguageSupported(voice.languages || [], 'en') ? <div className="extension-compatibility">{t('English support is not declared for this voice.')}</div> : null}
          </li>)}
          {nativeContributions.map(item => <li key={`${item.kind}-${item.id}`}>
            <div className="extension-contribution-heading"><strong>{item.label}{item.title ? ` · ${extensionText(item.title, locale)}` : ''}</strong>
              <span>{t('Work Agents')}: {extension.providers.length ? extension.providers.map(id => providers.find(provider => provider.provider_id === id)?.display_name || id).join(', ') : t('Not bound')}</span>
            </div>
          </li>)}
        </ul>
        <div className="extension-card-bottom">
          <details className="extension-details">
            <summary>{t('Plugin details')}</summary>
            <div className="extension-details-body">
              <dl className="extension-facts">
                <div><dt>{t('Package ID')}</dt><dd>{extension.id}</dd></div>
                <div><dt>{t('Saved state')}</dt><dd>{t(savedState)}</dd></div>
                <div><dt>{t(frontend ? 'Current appearance' : 'Current backend')}</dt><dd>{t(currentState)}</dd></div>
                {extension.revision !== undefined ? <div><dt>{t('Saved revision')} · {t('Loaded revision')}</dt><dd>{extension.revision} · {extension.appliedRevision ?? '—'}</dd></div> : null}
                {observed && observed.version !== extension.version ? <div><dt>{t('Running version')}</dt><dd>{observed.version}</dd></div> : null}
                {themes.length && appliedThemeVersion ? <div><dt>{t('Running theme version')}</dt><dd>{appliedThemeVersion}</dd></div> : null}
              </dl>
              {observed?.detail && !needsAttention ? <div className="settings-field-description extension-detail">{t(observed.detail)}</div> : null}
              {observed?.contributions.length ? <ul className="extension-diagnostics">
                {observed.contributions.map(item => <li key={`${item.kind}-${item.id}`}><strong>{item.id}</strong> · {t(extensionRuntimeLabel(item.health))}{item.detail ? ` · ${t(item.detail)}` : ''}</li>)}
              </ul> : null}
              {nativeContributions.length ? <div className="settings-field-description">{t('Main Chat')}: {t('No access')}</div> : null}
              <div><div className="settings-field-label">{t('Declared access and commands')}</div>
                <pre className="extension-disclosure">{extensionDisclosure(extension.manifest)}</pre>
              </div>
              <SettingsButton tone="danger" size="small" disabled={!available || busy || extension.pendingRemoval} onClick={() => { if (canLeave()) { close(); setRemoveCandidate(extension.id) } }}>{t('Remove')}</SettingsButton>
            </div>
          </details>
        {editing ? (selected?.manifest && draft && selectedMetadataAvailable ? editorForm(extension) : null)
        : disableCandidate === extension.id ? <div className="settings-form-remove" role="group" aria-label={t('Disable extension')}>
          <span>{t(frontend ? 'Disable this package now?' : 'Disable this package after the backend restarts?')}</span>
          <div className="settings-field-description extension-removal-data">{t('This package contains the saved speech engine. After restarting, speech is unavailable while text responses continue. Choose an enabled voice to restore speech.')}</div>
          <SettingsButton disabled={busy} onClick={() => setDisableCandidate('')}>{t('Keep enabled')}</SettingsButton>
          <SettingsButton tone="danger" disabled={busy} onClick={() => void setEnabled(extension, false)}>{t('Disable')}</SettingsButton>
        </div>
        : removeCandidate === extension.id ? <div className="settings-form-remove" role="group" aria-label={t('Remove extension')}>
          <span>{t(frontend ? 'Remove this package now?' : 'Remove this package after the backend restarts?')}</span>
          {selectedVoice ? <div className="settings-field-description extension-removal-data">{t('This package contains the saved speech engine. After restarting, speech is unavailable while text responses continue. Choose an enabled voice to restore speech.')}</div> : null}
          <div className="extension-removal-data"><div className="settings-field-description">{t(frontend ? 'Package resources are removed now. Saved data is kept unless you choose to delete it.' : 'After restart, package code and saved credentials are removed. Its saved data is kept unless you choose to delete it. Retaining data does not keep the extension running.')}</div>
            <label><input type="checkbox" checked={deleteData} disabled={busy} onChange={event => setDeleteData(event.target.checked)} />{t('Also delete this extension’s saved data')}</label></div>
          <SettingsButton disabled={busy} onClick={() => setRemoveCandidate('')}>{t('Keep extension')}</SettingsButton>
          <SettingsButton tone="danger" disabled={busy} onClick={() => void remove(extension.id)}>{t('Confirm remove')}</SettingsButton>
        </div> : <div className="settings-form-actions-end">
          {themes.length ? <SettingsButton tone="quiet" disabled={busy} onClick={onOpenAppearance}>{t('Open appearance settings')}</SettingsButton> : null}
          {voices.length ? <SettingsButton tone="quiet" disabled={busy} onClick={onOpenVoice}>{t('Open voice settings')}</SettingsButton> : null}
          <SettingsButton disabled={!available || busy || extension.pendingRemoval} onClick={() => edit(extension)}>{t('Configure extension')}</SettingsButton>
        </div>}
        </div>
      </CardShell>
    })}
    {selected && draft && !selectedMetadataAvailable ? <div role="status" className="settings-field-description">{t('Extension metadata is unavailable. Your unsaved changes are retained until validation recovers.')}</div> : null}
    </div>
    </SettingsGroup>
    </section>
    <section id="extension-panel-browse" role="tabpanel" aria-labelledby="extension-tab-browse" hidden={tab !== 'browse'}>
      <ExtensionCatalog catalog={catalog} installed={extensions} busy={busy || Boolean(progress)} selection={catalogSelection} onSelect={setCatalogSelection} onInstall={entry => void installCatalog(entry)} onLink={(entry, field) => void openCatalogLink(entry, field)} />
    </section>
  </div>
}
