import { useEffect, useRef, useState } from 'react'
import type { ExtensionSummary, ExtensionMutationResult } from '../../shared/extensions.js'
import { extensionDisclosure } from '../../shared/extensions.js'
import { extensionNeedsAgent } from '../../shared/extensionProfile.js'
import { useI18n } from '../i18n'
import { CardShell, SettingsButton, SettingsField, StatusPill } from './SettingsPrimitives'
import Switch from './Switch'
import { canLeaveSettingsEditors, DISCARD_CONNECTION_CHANGES, type SettingsEditorState } from './settingsDraft'
import { compatibleExtensionProviders, extensionDraft, extensionLanguageSupported, extensionRuntimeLabel, extensionText, extensionUpdate,
  type ExtensionDraft, type ExtensionProvider, type ExtensionRuntimeStatus } from './extensionSettingsCatalog'

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
  const [removeCandidate, setRemoveCandidate] = useState('')
  const [deleteData, setDeleteData] = useState(false)
  const dirty = draft !== null && JSON.stringify(draft) !== initial.current
  useEffect(() => {
    onEditorStateChange?.({ dirty, busy })
    return () => onEditorStateChange?.({ dirty: false, busy: false })
  }, [dirty, busy, onEditorStateChange])
  useEffect(() => {
    if (!selected) return
    editor.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    editor.current?.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true })
  }, [selected])
  const canLeave = () => canLeaveSettingsEditors([{ dirty, busy }], () => window.confirm(t(DISCARD_CONNECTION_CHANGES)))
  const close = () => { setSelected(null); setDraft(null); setRemoveCandidate(''); setDeleteData(false) }
  const edit = (extension: ExtensionSummary) => {
    if (!extension.manifest || !canLeave()) return
    const next = extensionDraft(extension)
    initial.current = JSON.stringify(next)
    // Capture the saved baseline. Runtime and settings refreshes cannot erase a draft.
    setSelected(extension)
    setDraft(next)
    setRemoveCandidate('')
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
  const missingProviders = draft?.providers.filter(id => !compatibleProviders.some(provider => provider.provider_id === id)) || []

  return <div className="settings-embedded-stack extension-settings">
    <CardShell vertical>
      <div className="settings-note-title">{t('Local community packages')}</div>
      <div className="settings-note-description">{t('Install a local wheel from an author you trust. Frontend-only packages apply immediately. Packages with backend contributions apply together after backend restart.')}</div>
      <div className="settings-note-description">{t('This pilot supports themes, TTS, MCP servers and Agent Skills. Install once, then choose a theme, voice or compatible Agents. Main Chat has no tool or Skill access.')}</div>
      <div className="settings-form-actions-end"><SettingsButton tone="primary" disabled={!available || busy} onClick={() => void install()}>{t(busy && !selected ? 'Working…' : 'Install or update local wheel…')}</SettingsButton></div>
    </CardShell>
    {!available ? <div className="settings-field-description">{t('Extension management is available in the Electron app.')}</div> : null}
    {error ? <div role="alert" className="settings-form-error">{t(error)}</div> : null}
    {notice ? <div role="status" className="settings-form-save-hint">{t(notice)}</div> : null}
    {available && !extensions.length ? <div className="settings-field-description">{t('No community extensions installed.')}</div> : null}
    {extensions.map(extension => {
      if (!extension.manifest) return <CardShell vertical key={extension.id}>
        <div className="extension-card-heading"><div className="settings-card-title extension-package-identity">{extension.id} · {extension.version}</div>
          <StatusPill ok={false}>{t(extension.status === 'incompatible' ? 'Incompatible' : 'Extension unavailable')}</StatusPill></div>
        <div role="alert" className="settings-field-description">{t(extension.validationError || 'Extension metadata is unavailable.')}</div>
        {!extension.pendingRemoval && <div className="settings-form-actions-end">
          <SettingsButton tone="danger" disabled={!available || busy} onClick={() => {
            if (canLeave() && window.confirm(t('Remove extension? Saved data will be retained.'))) void remove(extension.id, false)
          }}>{t('Remove')}</SettingsButton>
        </div>}
      </CardShell>
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
      return <CardShell vertical key={extension.id}>
        <div className="extension-card-heading">
          <div className="min-w-0"><h4 className="settings-card-title">{extensionText(extension.manifest.display_name, locale, extension.id)}</h4>
            <div className="settings-card-description extension-package-identity">{extension.id} · {extension.version}</div></div>
          <StatusPill ok={false} tone={extension.restartRequired || extension.pendingRemoval ? 'warning' : 'neutral'}>{t(extension.restartRequired ? 'Restart required' : savedState)}</StatusPill>
        </div>
        <div className="settings-card-description">{extensionText(extension.manifest.description, locale)}</div>
        <details className="settings-field-description"><summary>{t('Declared access and commands')}</summary>
          <pre className="whitespace-pre-wrap wrap-anywhere">{extensionDisclosure(extension.manifest)}</pre>
        </details>
        <div className="settings-connection-facts">
          <span><strong>{t('Saved state')}:</strong> {t(savedState)}</span>
          {frontend ? <span><strong>{t('Current appearance')}:</strong> {t(extension.applicationError ? 'Unavailable' : extension.enabled && extension.appliedRevision === extension.revision ? 'Applied' : 'Disabled')}</span>
            : <span><strong>{t('Current backend')}:</strong> <StatusPill ok={observed?.state === 'active'} tone={!observed ? 'neutral' : undefined}>{t(extensionRuntimeLabel(observed?.state))}</StatusPill></span>}
          {extension.revision !== undefined ? <span>{t('Saved revision')}: {extension.revision} · {t('Loaded revision')}: {extension.appliedRevision ?? '—'}</span> : null}
          {observed && observed.version !== extension.version ? <span>{t('Running version')}: {observed.version}</span> : null}
        </div>
        {extension.frontendSuppressed && extension.backendStillRunning ? <div role="status" className="settings-field-description">{t('Frontend access is closed; the previous backend still runs until restart.')}</div> : null}
        {extension.applicationError ? <div role="alert" className="settings-form-error">{extension.applicationError}</div> : null}
        {observed?.detail ? <div className="settings-field-description extension-detail">{t(observed.detail)}</div> : null}
        <ul className="extension-contributions" aria-label={t('Contributions')}>
          {themes.map(theme => <li key={`theme-${theme.id}`}>
            <strong>{t('Theme')} · {extensionText(theme.title, locale, theme.id)}</strong>
            <div>{t('Base theme')}: {t(theme.base === 'classic' ? 'Classic light' : 'Wallpaper slice')}</div>
            {selectedTheme === `${extension.id}/${theme.id}` ? <div>{t('Saved theme')}{appliedThemeVersion ? ` · ${t('Running version')}: ${appliedThemeVersion}` : ` · ${t('Unavailable')}`}</div> : null}
          </li>)}
          {voices.map(voice => <li key={`voice-${voice.id}`}>
            <strong>{t('Speech synthesis')} · {extensionText(voice.title, locale, voice.id)}</strong>
            <div>{t(voice.deployment === 'remote' ? 'Remote speech service' : 'Local speech engine')} · {t('Declared languages')}: {(voice.languages || []).join(', ')}</div>
            <div>{t('Amadeus speech languages: Japanese and English.')}</div>
            {!extensionLanguageSupported(voice.languages || [], 'ja') ? <div>{t('Japanese support is not declared. Select English for this voice; Japanese output has not been verified.')}</div> : null}
            {!extensionLanguageSupported(voice.languages || [], 'en') ? <div>{t('English support is not declared for this voice.')}</div> : null}
            {(() => { const status = observed?.contributions.find(item => item.kind === 'voice_backend' && item.id === voice.id)
              return status ? <div>{t('Current backend')}: {t(extensionRuntimeLabel(status.health))}{status.detail ? ` · ${t(status.detail)}` : ''}</div> : null })()}
          </li>)}
          {nativeContributions.map(server => <li key={`${server.kind}-${server.id}`}>
            <strong>{server.label} · {extensionText(server.title, locale, server.id)}</strong>
            <div>{t('Providers')}: {extension.providers.length ? extension.providers.join(', ') : t('Not bound')} · {t('Main Chat')}: {t('No access')}</div>
            {(() => { const status = observed?.contributions.find(item => item.kind === server.kind && item.id === server.id)
              return status ? <div>{t('Current backend')}: {t(extensionRuntimeLabel(status.health))}{status.detail ? ` · ${t(status.detail)}` : ''}</div> : null })()}
          </li>)}
        </ul>
        {removeCandidate === extension.id && selected?.id !== extension.id ? <div className="settings-form-remove" role="group" aria-label={t('Remove extension')}>
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
          <SettingsButton tone="danger" disabled={!available || busy || extension.pendingRemoval} onClick={() => { if (canLeave()) { close(); setRemoveCandidate(extension.id) } }}>{t('Remove')}</SettingsButton>
        </div>}
      </CardShell>
    })}
    {selected && draft && !selectedMetadataAvailable ? <div role="status" className="settings-field-description">{t('Extension metadata is unavailable. Your unsaved changes are retained until validation recovers.')}</div> : null}
    {selected?.manifest && draft && selectedMetadataAvailable ? <form ref={editor} className="settings-connection-editor settings-form" onSubmit={event => { event.preventDefault(); void save() }} aria-label={t('Configure extension')}>
      <div className="settings-connection-editor-heading"><h4 className="settings-card-title">{extensionText(selected.manifest.display_name, locale, selected.id)}</h4>
        <div className="settings-card-description">{selected.id} · {selected.version}</div></div>
      <fieldset className="settings-form-body" disabled={busy}>
        <SettingsField id="extension-enabled" label="Enable extension" description={selected.application === 'frontend' ? 'Applies immediately after saving.' : 'Applies after saving and restarting the backend.'}>
          <Switch id="extension-enabled" describedBy="extension-enabled-hint" checked={draft.enabled} onChange={enabled => change({ enabled })} />
        </SettingsField>
        {!draft.enabled && (selected.manifest.contributes.voice_backend || []).some(voice => `${selected.id}/${voice.id}` === selectedVoiceBackend) ? <div className="settings-field-description">{t('This package contains the saved speech engine. After restarting, speech is unavailable while text responses continue. Choose an enabled voice to restore speech.')}</div> : null}
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
                required={draft.enabled && field.required && !selected.secretConfigured[key]}
                onChange={event => change({ secrets: { ...draft.secrets, [key]: event.target.value } })} />
              {selected.secretConfigured[key] || draft.secrets[key] ? <SettingsButton tone="danger" disabled={!encryptionAvailable} onClick={() => change({ secrets: { ...draft.secrets, [key]: null } })}>{t('Clear')} {label}</SettingsButton> : null}
              {!encryptionAvailable ? <div className="settings-field-description">{t('Secure credential storage is unavailable. Credentials cannot be saved on this device.')}</div> : null}
            </div> : field.type === 'boolean' ? <Switch id={id} describedBy={description ? `${id}-hint` : undefined} checked={draft.values[key] === true}
                onChange={value => change({ values: { ...draft.values, [key]: value } })} />
              : field.type === 'enum' ? <select id={id} className="settings-form-input" value={String(draft.values[key] ?? '')} required={draft.enabled && field.required}
                aria-describedby={description ? `${id}-hint` : undefined} onChange={event => change({ values: { ...draft.values, [key]: event.target.value } })}>
                <option value="">{t('Not set')}</option>
                {(field.options || []).map(option => { const value = typeof option === 'string' ? option : option.value
                  return <option value={value} key={value}>{typeof option === 'string' ? option : extensionText(option.label, locale, value)}</option> })}
              </select> : <input id={id} className="settings-form-input" type={field.type === 'url' ? 'url' : field.type === 'integer' || field.type === 'number' ? 'number' : 'text'}
                value={String(draft.values[key] ?? '')} min={field.min} max={field.max} step={field.type === 'integer' ? 1 : 'any'} maxLength={field.max_length}
                required={draft.enabled && field.required} aria-describedby={description ? `${id}-hint` : undefined}
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
          <button type="submit" className="settings-button" data-tone="primary" disabled={busy || !dirty}>{t(busy ? 'Saving…' : 'Save extension')}</button></div>
      </div>
    </form> : null}
  </div>
}
