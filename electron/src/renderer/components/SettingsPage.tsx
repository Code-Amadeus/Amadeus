import { projectStartupFields, startupValues } from '../../shared/startupSettings.js'
import { catalogApplication, catalogConfiguration, catalogGroups, desktopCatalogFields, optionsWithCurrentValue, runtimeCatalogFields } from '../../shared/configCatalog.js'
import { settingSourceLabel } from '../../shared/characterStartup'
import { useState, useEffect, useCallback, useMemo, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import { GroupTitle, CardShell, CardIcon, StatusPill, SettingsGroup } from './SettingsPrimitives'
import McpConnections, { type McpConnectionSummary } from './McpConnections'
import ChatAvatarSettings from './ChatAvatarSettings'
import CharacterVisualsPage from './CharacterVisualsPage'
import CharacterPage from './CharacterPage'
import { CHARACTER_SECTION_KEY, characterSection, type CharacterSection } from './characterWorkspace'
import MainChatCharacterSettings from './MainChatCharacterSettings'
import CharacterManagementSettings from './CharacterManagementSettings'
import BackendStartupRecovery from './BackendStartupRecovery'
import RetiredRouteSetting, { RETIRED_ROUTE_KEY, retiredRouteMigration, removeStoredRetiredRouteSetting, type RetiredSettingFact } from './RetiredRouteSetting'
import AcpProviders, { type AcpConfiguration } from './AcpProviders'
import CapabilitiesPanel, { RuntimePackages, type RuntimePackageStatus } from './CapabilitiesPanel'
import { buildCapabilityProfiles, type SceneConfigureSection } from './sceneCapabilityProjection'
import { useI18n, type UiLocale } from '../i18n'
import { useTheme, type UiTheme } from '../theme'
import { chatProviderSupportsImages } from './chatModelCapabilities'
import {
  buildLocalModelConnectionCatalog,
  buildOptionalModelServiceCatalog,
  buildRemoteModelConnectionCatalog,
} from './modelConnectionCatalog'
import { buildVoiceConfigurationCatalog, voiceConfigurationSections } from './voiceConfigurationCatalog'
import { buildWorkProviderCatalog } from './providerConnectionCatalog'
import { buildModelRoleCatalog } from './modelRoleCatalog'
import { buildGraphicsConfiguration, type GraphicsRuntimeSettings } from './graphicsConfigurationCatalog'
import { markRuntimeSettingsApplied, persistDesktopRuntimeSettings, runtimeSettingFromDesktopValues, runtimeSettingValue } from './desktopRuntimeSettings'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
  reconnectBackend: () => Promise<void>
}

type SettingsSection = 'capabilities' | 'characters' | 'graphics' | SceneConfigureSection
type ModelsPage = 'roles' | 'connections'

type StartupOption = string | { value: string; label: string }

interface StartupField {
  key: string
  label: string
  type: 'text' | 'url' | 'path' | 'number' | 'select' | 'boolean' | 'secret'
  description?: string
  value?: string | boolean
  configured?: boolean
  options?: StartupOption[]
  min?: number
  max?: number
  step?: number
  editable: boolean
  restart_required: boolean
}

interface ConfigurationGroup {
  id: string
  label: string
  description?: string
  active?: boolean
  configured?: boolean
  status?: string
  status_ok?: boolean
  status_detail?: string
  fields: StartupField[]
}

interface ProviderAvailability {
  provider_id: string
  configured: boolean
  ready: boolean
  registered: boolean
  reason: string
  version?: string
  diagnostic?: string
  authentication?: string
}

interface ProviderManifest {
  provider_id: string
  display_name: string
  runtime_kind: string
  capabilities?: { capability_projections?: string[] }
}

interface CapabilityBinding {
  surface: string
  projection: string
  enabled: boolean
}

interface CapabilityContribution {
  kind: 'provider' | 'mcp_server' | 'skill' | 'auip_app'
  id: string
  summary: string
  available: boolean
  health: string
  health_detail?: string
  consumer_scope?: string
  bindings?: CapabilityBinding[]
  requirements?: string[]
  metadata?: Record<string, unknown>
}

interface CapabilityPackage {
  id: string
  version: string
  source: string
  trust: string
  contributions: CapabilityContribution[]
}

interface DesktopSettingsSnapshot {
  platform: string
  startupValues?: Record<string, string>
  values: Record<string, string>
  sources: Record<string, 'environment' | 'user' | 'dotenv' | 'default'>
  locked: Record<string, boolean>
  secrets: Record<string, { configured: boolean; source: string; locked: boolean }>
  encryptionAvailable: boolean
  mcpConnections: McpConnectionSummary[]
  mcpConnectionsLocked: boolean
  restartRequired: boolean
  pendingKeys: string[]
  pendingRevisions: Record<string, number>
  retired_settings: RetiredSettingFact[]
}

interface CompanionPortraitStatus {
  installed: boolean
  state: 'ready' | 'not_installed' | 'incomplete' | 'invalid'
  emotionCount: number
  frameCount: number
  detail: string
}

interface VisionWindowItem {
  hwnd: string
  title: string
  processName: string
  selected: boolean
}

function asVisionWindows(value: unknown): VisionWindowItem[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    const source = asRecord(item)
    const hwnd = String(source.hwnd ?? '').trim()
    const title = String(source.title ?? '').trim()
    if (!hwnd || !title) return []
    return [{
      hwnd,
      title,
      processName: String(source.processName ?? '').trim(),
      selected: Boolean(source.selected),
    }]
  })
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function asConfigurationGroups(value: unknown): ConfigurationGroup[] {
  return Array.isArray(value) ? value as ConfigurationGroup[] : []
}


type ComboOption = string | { value: string; label: string }

function ComboCard({ icon, title, content, value, onChange, options, disabled }: {
  icon: FluentIconName; title: string; content: string; value: string
  onChange: (v: string) => void; options: ComboOption[]; disabled?: boolean
}) {
  const { t } = useI18n()
  return (
    <CardShell>
      <CardIcon name={icon} />
      <div className="flex-1 min-w-0" style={{ paddingRight: 16 }}>
        <div className="settings-card-title">{t(title)}</div>
        {content ? <div className="settings-card-description">{t(content)}</div> : null}
      </div>
      <select
        aria-label={t(title)}
        value={value}
        onChange={event => onChange(event.target.value)}
        disabled={disabled}
        className="text-[12px] bg-[var(--surface-alt)] border border-[var(--border)] rounded-lg px-3 text-[var(--text)] outline-none hover:border-[var(--border-strong)] focus:border-[var(--accent)] disabled:opacity-40 shrink-0"
        style={{ minWidth: 145, height: 35 }}
      >
        {optionsWithCurrentValue(options, value).map(option => {
          const optionValue = typeof option === 'string' ? option : option.value
          const label = typeof option === 'string' ? option : option.label
          return <option key={optionValue} value={optionValue}>{t(label)}</option>
        })}
      </select>
    </CardShell>
  )
}

function SwitchCard({ icon, title, content, checked, onChange, disabled }: {
  icon: FluentIconName; title: string; content: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean
}) {
  const { t } = useI18n()
  return (
    <CardShell>
      <CardIcon name={icon} />
      <div className="flex-1 min-w-0" style={{ paddingRight: 16 }}>
        <div className="settings-card-title">{t(title)}</div>
        {content ? <div className="settings-card-description">{t(content)}</div> : null}
      </div>
      <button
        onClick={() => onChange(!checked)}
        disabled={disabled}
        className="relative shrink-0 transition-colors cursor-pointer"
        style={{ width: 38, height: 22, borderRadius: 11, backgroundColor: checked ? 'var(--accent)' : 'var(--border-strong)', border: 'none' }}
        aria-pressed={checked}
        aria-label={t(title)}
      >
        <span className="absolute top-[3px] left-0 w-4 h-4 rounded-full bg-white shadow transition-transform" style={{ transform: checked ? 'translateX(19px)' : 'translateX(3px)' }} />
      </button>
      <span className="text-[11px] shrink-0 ml-2" style={{ color: 'var(--muted)', width: 28 }}>{t(checked ? 'On' : 'Off')}</span>
    </CardShell>
  )
}

function RoleAssignmentCard({
  icon,
  title,
  description,
  assignment,
  policy,
  status,
  statusOk,
  onConfigure,
  configureLabel = 'Open model connections',
}: {
  icon: FluentIconName
  title: string
  description: string
  assignment: string
  policy: string
  status: string
  statusOk: boolean
  onConfigure?: () => void
  configureLabel?: string
}) {
  const { t } = useI18n()
  return (
    <div className="setting-card model-role-card">
      <span className="model-role-icon" aria-hidden="true"><FluentIcon name={icon} size={17} /></span>
      <div className="model-role-copy">
        <div className="settings-card-title">{t(title)}</div>
        <div className="settings-card-description">{t(description)}</div>
      </div>
      <div className="model-role-meta">
        <div className="model-role-assignment-line">
          <strong title={t(assignment)}>{t(assignment)}</strong>
          {onConfigure ? (
            <button type="button" className="capability-configure-button" onClick={onConfigure} aria-label={t(configureLabel)} title={t(configureLabel)}>
              <FluentIcon name="Setting" size={14} />
            </button>
          ) : null}
        </div>
        <div className="model-role-status-line">
          <span>{t(policy)}</span>
          <StatusPill ok={statusOk}>{t(status)}</StatusPill>
        </div>
      </div>
    </div>
  )
}

function InlineFieldAction({ label, glyph, busy = false, tone = 'normal', disabled, onClick }: {
  label: string
  glyph: string
  busy?: boolean
  tone?: 'normal' | 'danger'
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      data-tone={tone}
      className="settings-inline-action flex items-center justify-center rounded-md disabled:opacity-30"
      style={{ width: 27, height: 27, border: 0, background: 'transparent' }}
    >
      {busy
        ? <FluentIcon name="Sync" size={13} className="animate-spin" />
        : <span aria-hidden="true" style={{ fontSize: glyph === '×' ? 18 : 15, lineHeight: 1 }}>{glyph}</span>}
    </button>
  )
}

function StartupFieldRow({ field, desktop, onSave }: {
  field: StartupField
  desktop: DesktopSettingsSnapshot | null
  onSave: (field: StartupField, value: string | boolean | null, secret: boolean) => Promise<void>
}) {
  const { t } = useI18n()
  const source = field.key ? desktop?.sources?.[field.key] || 'default' : 'default'
  const initial = field.value ?? ''
  const unknown = field.type !== 'secret' && field.value === undefined
  const [draft, setDraft] = useState<string | boolean>(initial)
  const [busy, setBusy] = useState(false)
  const [secretDraft, setSecretDraft] = useState('')
  const electronUnavailable = !desktop
  const environmentLocked = field.key ? Boolean(desktop?.locked?.[field.key]) : true
  const locked = electronUnavailable || environmentLocked
  const secretConfigured = field.type === 'secret'
    ? Boolean(desktop?.secrets?.[field.key]?.configured ?? field.configured)
    : false

  useEffect(() => {
    setDraft(initial)
  }, [initial])

  const save = async (value: string | boolean | null, secret: boolean) => {
    setBusy(true)
    try {
      await onSave(field, value, secret)
      if (secret) setSecretDraft('')
    } catch {
      if (!secret) setDraft(initial)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="settings-field-row flex items-start gap-5" style={{ padding: '10px 0', borderTop: '1px solid var(--divider)' }}>
      <div className="flex-1 min-w-0">
        <div className="settings-field-label">{t(field.label)}</div>
        <div className="settings-field-description">
          {field.description ? `${t(field.description)} · ` : ''}{t(settingSourceLabel(source))}{electronUnavailable ? ` · ${t('editable in Electron app')}` : environmentLocked ? ` · ${t('locked')}` : ''}{field.restart_required ? ` · ${t('restart required')}` : ''}{unknown ? ` · ${t('Value unavailable until backend connects')}` : ''}
        </div>
      </div>
      <div className="settings-field-control flex items-center gap-1.5 shrink-0" style={{ width: 320, maxWidth: '43%' }}>
        {!field.editable || !field.key ? (
          <span className="text-[13px] truncate ml-auto" style={{ color: 'var(--text)' }}>{String(field.value ?? '')}</span>
        ) : field.type === 'secret' ? (
          <div className="relative min-w-0 flex-1">
            <input
              aria-label={t(field.label)}
              type="password"
              value={secretDraft}
              onChange={event => setSecretDraft(event.target.value)}
              disabled={locked || busy}
              placeholder={t(secretConfigured ? 'Configured — enter to replace' : 'Not configured')}
              autoComplete="new-password"
              onKeyDown={event => {
                if (event.key === 'Enter' && secretDraft && !locked && !busy) {
                  void save(secretDraft, true)
                }
              }}
              onBlur={event => {
                const next = event.relatedTarget as Node | null
                if (next && event.currentTarget.parentElement?.contains(next)) return
                if (secretDraft && !locked && !busy) void save(secretDraft, true)
              }}
              className="w-full min-w-0 text-[12px] bg-[var(--surface-alt)] border border-[var(--border)] rounded-lg pl-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
              style={{
                height: 34,
                paddingRight: secretDraft && secretConfigured
                  ? 65
                  : secretDraft || secretConfigured ? 36 : 12,
              }}
            />
            {(secretDraft || secretConfigured) ? (
              <div className="absolute inset-y-0 right-1 flex items-center gap-0.5">
                {secretDraft ? (
                  <InlineFieldAction
                    label={`${t('Save')} ${t(field.label)}`}
                    glyph="✓"
                    busy={busy}
                    disabled={locked || busy}
                    onClick={() => void save(secretDraft, true)}
                  />
                ) : null}
                {secretConfigured ? (
                  <InlineFieldAction
                    label={`${t('Clear')} ${t(field.label)}`}
                    glyph="×"
                    tone="danger"
                    disabled={locked || busy}
                    onClick={() => void save(null, true)}
                  />
                ) : null}
              </div>
            ) : null}
          </div>
        ) : field.type === 'select' ? (
          <select
            aria-label={t(field.label)}
            value={String(draft)}
            onChange={event => {
              const value = event.target.value
              setDraft(value)
              if (value !== String(initial)) void save(value, false)
            }}
            disabled={locked || busy}
            className="min-w-0 flex-1 text-[12px] bg-[var(--surface-alt)] border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
            style={{ height: 34 }}
          >
            {unknown ? <option value="">{t('Unknown')}</option> : null}
            {(unknown ? field.options || [] : optionsWithCurrentValue(field.options || [], String(draft))).map(option => {
              const optionValue = typeof option === 'string' ? option : option.value
              const optionLabel = typeof option === 'string' ? option || 'Inherit' : option.label
              return <option key={optionValue} value={optionValue}>{t(optionLabel)}</option>
            })}
          </select>
        ) : field.type === 'boolean' ? (
          <select
            aria-label={t(field.label)}
            value={String(draft)}
            onChange={event => {
              const value = event.target.value === 'true'
              setDraft(value)
              if (String(value) !== String(initial)) void save(value, false)
            }}
            disabled={locked || busy}
            className="min-w-0 flex-1 text-[12px] bg-[var(--surface-alt)] border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
            style={{ height: 34 }}
          >
            {unknown ? <option value="">{t('Unknown')}</option> : null}
            <option value="true">{t('On')}</option>
            <option value="false">{t('Off')}</option>
          </select>
        ) : (
          <input
            aria-label={t(field.label)}
            type={field.type === 'url' ? 'url' : field.type === 'number' ? 'number' : 'text'}
            placeholder={unknown ? t('Unknown') : undefined}
            min={field.min}
            max={field.max}
            step={field.step}
            value={String(draft)}
            onChange={event => setDraft(event.target.value)}
            onBlur={() => {
              if (!locked && !busy && String(draft) !== String(initial)) void save(String(draft), false)
            }}
            onKeyDown={event => {
              if (event.key === 'Enter') event.currentTarget.blur()
            }}
            disabled={locked || busy}
            className="min-w-0 flex-1 text-[12px] bg-[var(--surface-alt)] border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
            style={{ height: 34 }}
          />
        )}
        {busy && field.type !== 'secret' ? <span className="text-[10px] shrink-0" style={{ color: 'var(--muted)' }}>{t('Saving…')}</span> : null}
      </div>
    </div>
  )
}

function ConfigurationCard({ group, desktop, availability, onSave, collapsible = false, defaultOpen = false, optionalWhenInactive = false, icon }: {
  group: ConfigurationGroup
  desktop: DesktopSettingsSnapshot | null
  availability?: ProviderAvailability
  onSave: (field: StartupField, value: string | boolean | null, secret: boolean) => Promise<void>
  collapsible?: boolean
  defaultOpen?: boolean
  optionalWhenInactive?: boolean
  icon?: FluentIconName
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(defaultOpen)
  useEffect(() => setExpanded(defaultOpen), [defaultOpen])
  const statusOk = group.status_ok ?? (availability ? availability.ready && availability.registered : Boolean(group.configured))
  const availabilityStatus = availability?.reason === 'pi_model_credentials_unavailable'
    ? 'Missing credentials'
    : availability?.reason?.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase())
  const statusText = availability
    ? statusOk ? 'Registered' : availabilityStatus || 'Unavailable'
    : optionalWhenInactive && !group.active && !group.configured
      ? 'Optional'
      : group.status
      ? group.status.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase())
      : group.configured ? 'Configured' : group.active ? 'Needs setup' : 'Optional'
  const neutralStatus = ['Optional', 'Off', 'Disabled', 'Inactive', 'Backend status unavailable'].includes(statusText)
  const header = (
      <div className="configuration-card-header flex items-start gap-2.5">
        <div className="flex items-center justify-center mt-0.5" style={{ width: 24, color: 'var(--muted)' }}>
          <FluentIcon name={icon || (group.id === 'local' ? 'CommandPrompt' : 'Robot')} size={17} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <div className="settings-card-title">{t(group.label)}</div>
            {group.active ? <span className="text-[9px] font-[700]" style={{ color: 'var(--accent)' }}>{t('ACTIVE')}</span> : null}
          </div>
          {group.description ? <div className="settings-card-description">{t(group.description)}</div> : null}
          {group.status_detail ? <div className="settings-meta-text">{t(group.status_detail)}</div> : null}
          {availability?.diagnostic && !statusOk ? <div className="settings-meta-text">{t(availability.diagnostic)}</div> : null}
        </div>
        <StatusPill ok={statusOk} tone={neutralStatus ? 'neutral' : undefined}>{t(statusText)}</StatusPill>
      </div>
  )
  const fields = (
    <>
      {group.fields.map(field => <StartupFieldRow key={`${group.id}-${field.key || field.label}`} field={field} desktop={desktop} onSave={onSave} />)}
      {group.fields.length === 0 ? <div className="text-[11px] pt-2" style={{ color: 'var(--muted)', borderTop: '1px solid var(--border)' }}>{t('No user-managed connection settings.')}</div> : null}
    </>
  )
  if (collapsible) {
    return (
      <details className="setting-card configuration-card-details" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
        <summary>{header}</summary>
        <div className="configuration-card-fields">{fields}</div>
      </details>
    )
  }
  return (
    <CardShell vertical>
      {header}
      <div className="configuration-card-fields">{fields}</div>
    </CardShell>
  )
}

function CapabilityCard({ contribution, packageInfo, consumers }: {
  contribution: CapabilityContribution
  packageInfo: CapabilityPackage
  consumers: string[]
}) {
  const { t } = useI18n()
  const bindings = (contribution.bindings || []).filter(binding => binding.enabled)
  return (
    <CardShell vertical>
      <div className="flex items-start gap-2.5">
        <div className="flex items-center justify-center mt-0.5" style={{ width: 22, color: 'var(--muted)' }}>
          <FluentIcon name={contribution.kind === 'skill' ? 'Work' : 'CommandPrompt'} size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="settings-card-title">{contribution.id}</span>
            <span className="text-[9px] uppercase tracking-wide" style={{ color: 'var(--muted)' }}>{contribution.kind === 'mcp_server' ? 'MCP' : 'Skill'}</span>
          </div>
          <div className="settings-card-description">{contribution.summary}</div>
        </div>
        <StatusPill ok={contribution.available}>{t(contribution.available ? 'Available' : contribution.health)}</StatusPill>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 mt-2.5 pt-2.5 text-[9.5px]" style={{ borderTop: '1px solid var(--divider)', color: 'var(--muted)' }}>
        <div><span className="font-[600]">{t('Consumers')}:</span> {consumers.length ? consumers.join(', ') : t('No active Provider binding')}</div>
        <div><span className="font-[600]">{t('Scope')}:</span> {t('Work Providers only')}</div>
        <div><span className="font-[600]">{t('Projection')}:</span> {bindings.map(item => item.projection).join(', ') || t('None')}</div>
        <div><span className="font-[600]">{t('Source')}:</span> {packageInfo.source} · {packageInfo.trust}</div>
      </div>
    </CardShell>
  )
}

function BoundaryNote({ title, children }: { title: string; children: ReactNode }) {
  const { t } = useI18n()
  return (
    <div
      className="rounded-lg"
      style={{
        padding: '10px 12px',
        border: '1px solid var(--border)',
        background: 'var(--focus-fill)',
      }}
    >
      <div className="settings-note-title">{t(title)}</div>
      <div className="settings-note-description">{typeof children === 'string' ? t(children) : children}</div>
    </div>
  )
}

function ThemePicker({ value, onChange, disabled }: { value: UiTheme; onChange: (theme: UiTheme) => void; disabled?: boolean }) {
  const { t } = useI18n()
  const choices = desktopCatalogFields.AMADEUS_UI_THEME.options!.map(option => ({
    id: (typeof option === 'string' ? option : option.value) as UiTheme,
    title: typeof option === 'string' ? option : option.label['en-US'],
  }))
  return (
    <div className="settings-theme-picker" role="radiogroup" aria-label={t(desktopCatalogFields.AMADEUS_UI_THEME.title['en-US'])}>
      {choices.map(choice => {
        const selected = value === choice.id
        return (
          <button
            key={choice.id}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            className="settings-theme-option"
            data-preview-theme={choice.id}
            data-selected={selected ? 'true' : undefined}
            onClick={() => onChange(choice.id)}
          >
            <span className="settings-theme-preview" aria-hidden="true">
              <span className="settings-theme-preview-rail" />
              <span className="settings-theme-preview-stage">
                <span className="settings-theme-preview-heading" />
                <span className="settings-theme-preview-card"><i /><b /></span>
                <span className="settings-theme-preview-card compact"><i /><b /></span>
              </span>
            </span>
            <span className="settings-theme-option-copy">
              <strong>{t(choice.title)}</strong>
            </span>
            <span className="settings-theme-radio" aria-hidden="true"><i /></span>
          </button>
        )
      })}
    </div>
  )
}

export default function SettingsPage({ send, subscribe, connected, reconnectBackend }: Props) {
  const { locale, setLocale, t } = useI18n()
  const { theme, setTheme } = useTheme()
  const [section, setSection] = useState<SettingsSection>(() => {
    const saved = window.localStorage.getItem('amadeus.settings.section')
    if (saved === 'visuals') return 'characters'
    return ['capabilities', 'general', 'characters', 'graphics', 'models', 'voice', 'providers'].includes(String(saved))
      ? saved as SettingsSection
      : 'capabilities'
  })
  const [characterTab, setCharacterTab] = useState<CharacterSection>(() =>
    window.localStorage.getItem('amadeus.settings.section') === 'visuals' ? 'appearance'
      : characterSection(window.localStorage.getItem(CHARACTER_SECTION_KEY)))
  const openCharacters = useCallback((tab: CharacterSection) => { setCharacterTab(tab); setSection('characters') }, [])
  const [modelsPage, setModelsPage] = useState<ModelsPage>('roles')
  const [advancedRolesOpen, setAdvancedRolesOpen] = useState(false)
  const [config, setConfig] = useState<Record<string, unknown>>({})
  const [providerAvailability, setProviderAvailability] = useState<ProviderAvailability[]>([])
  const [providerManifests, setProviderManifests] = useState<ProviderManifest[]>([])
  const [providerRoleCandidates, setProviderRoleCandidates] = useState<Record<string, string[]> | undefined>()
  const [acpAgents, setAcpAgents] = useState('[]')
  const [acpConfigurations, setAcpConfigurations] = useState<AcpConfiguration[]>([])
  const [capabilityPackages, setCapabilityPackages] = useState<CapabilityPackage[]>([])
  const [desktop, setDesktop] = useState<DesktopSettingsSnapshot | null>(null)
  const [companionPortraits, setCompanionPortraits] = useState<CompanionPortraitStatus | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  const [restartPending, setRestartPending] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [visionWindows, setVisionWindows] = useState<VisionWindowItem[]>([])
  const [visionWindowsLoading, setVisionWindowsLoading] = useState(false)

  useEffect(() => { window.localStorage.setItem('amadeus.settings.section', section) }, [section])
  useEffect(() => { window.localStorage.setItem(CHARACTER_SECTION_KEY, characterTab) }, [characterTab])

  const refreshDesktop = useCallback(async () => {
    const snapshot = await window.amadeus?.getDesktopSettings()
    if (snapshot) setDesktop(snapshot as unknown as DesktopSettingsSnapshot)
  }, [])

  const refreshCompanionPortraits = useCallback(async () => {
    const status = await window.amadeus?.getCompanionPortraitStatus()
    if (status) setCompanionPortraits(status as unknown as CompanionPortraitStatus)
  }, [])

  const refreshBackend = useCallback(async () => {
    const [configResponse, providerResponse, capabilityResponse] = await Promise.all([
      send('system.get_config', {}),
      send('provider.list', {}),
      send('capability.list', { include_disabled: true }),
    ])
    setConfig((configResponse.values as Record<string, unknown>) ?? configResponse)
    setAcpAgents(JSON.stringify(providerResponse.acp_agents || []))
    setAcpConfigurations((providerResponse.provider_configurations || []) as AcpConfiguration[])
    setProviderAvailability(Array.isArray(providerResponse.provider_availability) ? providerResponse.provider_availability as unknown as ProviderAvailability[] : [])
    setProviderManifests(Array.isArray(providerResponse.provider_manifests) ? providerResponse.provider_manifests as unknown as ProviderManifest[] : [])
    setProviderRoleCandidates(providerResponse.role_candidates as Record<string, string[]> | undefined)
    setCapabilityPackages(Array.isArray(capabilityResponse.packages) ? capabilityResponse.packages as unknown as CapabilityPackage[] : [])
  }, [send])

  useEffect(() => {
    void Promise.all([refreshDesktop(), refreshCompanionPortraits()])
      .catch(reason => setError(reason instanceof Error ? reason.message : 'Could not load desktop settings'))
    if (!connected) return undefined
    const unsubscribe = subscribe('system.config', payload => {
      setConfig((payload.values as Record<string, unknown>) ?? payload)
    })
    void refreshBackend().catch(reason => setError(reason instanceof Error ? reason.message : 'Could not load runtime settings'))
    return unsubscribe
  }, [connected, subscribe, refreshBackend, refreshDesktop, refreshCompanionPortraits])

  useEffect(() => {
    setRestartPending(Boolean(desktop?.restartRequired))
  }, [desktop?.restartRequired])

  const handleChange = useCallback(async (key: string, value: unknown) => {
    setSaving(key)
    setError('')
    setNotice('')
    let persisted = false
    try {
      const saved = await persistDesktopRuntimeSettings({ [key]: value })
      persisted = saved.persisted
      if (saved.settings) setDesktop(saved.settings as unknown as DesktopSettingsSnapshot)
      const response = await send('system.set_config', { values: { [key]: value } })
      setConfig((response.values as Record<string, unknown>) ?? response)
      const applied = await markRuntimeSettingsApplied({ [key]: value }, saved.pendingRevisions)
      if (applied) setDesktop(applied as unknown as DesktopSettingsSnapshot)
      if (key === 'main_chat_character_prompt_ja') setNotice('Kurisu Japanese persona saved. Later Japanese Main Chat and AUIP/browser requests use it when Kurisu is active.')
      return true
    } catch (reason) {
      if (persisted) {
        setNotice('Saved for the next backend start; the current runtime did not change.')
        return true
      } else {
        setError(reason instanceof Error ? reason.message : `Could not update ${key}`)
        return false
      }
    } finally {
      setSaving(null)
    }
  }, [send])

  const handleStartupSave = useCallback(async (
    field: StartupField,
    value: string | boolean | null,
    secret: boolean,
  ) => {
    if (!window.amadeus || !field.key) return
    setSaving(field.key)
    setError('')
    setNotice('')
    try {
      const result = await window.amadeus.updateDesktopSettings(
        secret ? { secrets: { [field.key]: value as string | null } } : { values: { [field.key]: value } },
      )
      if (!result.ok) throw new Error(result.error || `Could not save ${field.key}`)
      if (result.settings) setDesktop(result.settings as unknown as DesktopSettingsSnapshot)
      const runtimeField = Object.entries(runtimeCatalogFields).find(([, definition]) => definition.key === field.key)
      if (!secret && runtimeField && catalogApplication(field.key) === 'host') {
        const snapshot = result.settings as unknown as DesktopSettingsSnapshot
        const next = runtimeSettingValue(runtimeField[0], snapshot)
        if (next === undefined) {
          setNotice('Saved for the next backend start; the current runtime did not change.')
          return
        }
        try {
          const values = { [runtimeField[0]]: next }
          const response = await send('system.set_config', { values })
          setConfig((response.values as Record<string, unknown>) ?? response)
          const applied = await markRuntimeSettingsApplied(values, snapshot.pendingRevisions || {})
          if (applied) setDesktop(applied as unknown as DesktopSettingsSnapshot)
        } catch {
          setNotice('Saved for the next backend start; the current runtime did not change.')
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not save ${field.key}`)
      throw reason
    } finally {
      setSaving(null)
    }
  }, [send])

  const confirmRetiredRoute = useCallback(async () => {
    if (!window.amadeus) return
    setSaving(RETIRED_ROUTE_KEY)
    setError('')
    try {
      const saved = await removeStoredRetiredRouteSetting<DesktopSettingsSnapshot>(
        async request => {
          const result = await window.amadeus!.updateDesktopSettings(request)
          return { ...result, settings: result.settings as unknown as DesktopSettingsSnapshot | undefined }
        },
      )
      // Successful durable deletion is the only acknowledgment. A failure
      // leaves this snapshot and its notice intact, including after restart.
      setDesktop(saved)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not confirm the retired setting migration')
    } finally {
      setSaving(null)
    }
  }, [])

  const handleMainProviderChange = useCallback(async (value: string) => {
    await handleChange('llm_provider', value)
  }, [handleChange])

  const restartBackend = useCallback(async () => {
    if (!window.amadeus) return
    setRestarting(true)
    setError('')
    setNotice('')
    try {
      const ok = await window.amadeus.restartBackend()
      if (!ok) throw new Error('Backend restart failed')
      setNotice('Backend restarted; reconnecting to confirm saved settings…')
      await reconnectBackend()
      await Promise.all([refreshDesktop(), refreshBackend()])
      setNotice('Backend restarted and saved settings are now active.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Backend restart failed')
    } finally {
      setRestarting(false)
    }
  }, [reconnectBackend, refreshBackend, refreshDesktop])

  const handleVisionEnabled = useCallback(async (value: boolean) => {
    const currentMode = runtimeSettingValue('vision_mode', desktop, connected ? config.vision_mode : undefined)
    const nextMode = value && currentMode === 'off' ? 'on_demand' : currentMode
    const values = value && currentMode === 'off'
      ? { vision_enabled: true, vision_mode: nextMode }
      : { vision_enabled: value }
    setSaving('vision_enabled')
    setError('')
    setNotice('')
    let persisted = false
    try {
      const saved = await persistDesktopRuntimeSettings(values)
      persisted = saved.persisted
      if (saved.settings) setDesktop(saved.settings as unknown as DesktopSettingsSnapshot)
      const response = await send('system.set_config', { values })
      setConfig((response.values as Record<string, unknown>) ?? response)
      const applied = await markRuntimeSettingsApplied(values, saved.pendingRevisions)
      if (applied) setDesktop(applied as unknown as DesktopSettingsSnapshot)
    } catch (reason) {
      if (persisted) setNotice('Saved for the next backend start; the current runtime did not change.')
      else setError(reason instanceof Error ? reason.message : 'Could not update vision')
    } finally {
      setSaving(null)
    }
  }, [send, config, desktop, connected])

  const loadVisionWindows = useCallback(async () => {
    if (!connected) return
    setVisionWindowsLoading(true)
    try {
      const response = await send('system.list_windows', { limit: 48 })
      setVisionWindows(asVisionWindows(response.windows))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not list capture windows')
    } finally {
      setVisionWindowsLoading(false)
    }
  }, [connected, send])

  const handleVisionWindowTarget = useCallback(async (windowHandle: string) => {
    const values = { vision_scope: 'selected_window', vision_window_handle: windowHandle }
    setSaving('vision_window_handle')
    setError('')
    setNotice('')
    let persisted = false
    try {
      const saved = await persistDesktopRuntimeSettings(values)
      persisted = saved.persisted
      if (saved.settings) setDesktop(saved.settings as unknown as DesktopSettingsSnapshot)
      const response = await send('system.set_config', { values })
      setConfig((response.values as Record<string, unknown>) ?? response)
      const applied = await markRuntimeSettingsApplied(values, saved.pendingRevisions)
      if (applied) setDesktop(applied as unknown as DesktopSettingsSnapshot)
      setVisionWindows(current => current.map(item => ({ ...item, selected: item.hwnd === windowHandle })))
    } catch (reason) {
      if (persisted) setNotice('Saved for the next backend start; the current runtime did not change.')
      else setError(reason instanceof Error ? reason.message : 'Could not select capture window')
    } finally {
      setSaving(null)
    }
  }, [send])

  useEffect(() => {
    if (section === 'general' && Boolean(config.vision_enabled) && String(config.vision_scope ?? 'full_screen') === 'selected_window') {
      void loadVisionWindows()
    }
  }, [section, connected, config.vision_enabled, config.vision_scope, loadVisionWindows])

  const runtimeValue = (key: string, fallback?: string) => runtimeSettingValue(key, desktop, connected ? config[key] : undefined) ?? (runtimeCatalogFields[key] ? undefined : fallback)
  const val = (key: string, fallback?: string) => String(runtimeValue(key, fallback) ?? '')
  const bool = (key: string) => Boolean(runtimeValue(key))
  const runtimeControl = (key: string) => {
    const definition = runtimeCatalogFields[key]
    return {
      title: definition.title['en-US'],
      content: runtimeValue(key) === undefined ? 'Value unavailable until backend connects' : definition.description?.['en-US'] || '',
      disabled: saving === key || Boolean(desktop?.locked?.[definition.key]),
      options: [
        ...(runtimeValue(key) === undefined ? [{ value: '', label: 'Unknown' }] : []),
        ...(definition.options || []).map(option => typeof option === 'string' ? option : { value: option.value, label: option.label['en-US'] }),
      ],
    }
  }
  const startupConfiguration = catalogConfiguration('desktop_startup', desktop)
  const interfaceConfiguration = catalogConfiguration('desktop_interface', desktop)
  const renderRuntimeGroup = (id: string, icon: FluentIconName) => {
    const group = catalogGroups.find(group => group.id === id)!
    const context = Object.fromEntries(Object.entries(group.config).filter(([, field]) => field.computed_default)
      .map(([key, field]) => [key, runtimeValue(field.runtime_key!) as string | number | boolean | undefined]))
    const fields = catalogConfiguration(id, desktop, context).fields
    return Object.entries(group.config).map(([key, definition]) => {
      const runtimeKey = definition.runtime_key!
      if (runtimeKey === 'vision_window_handle' || runtimeKey === 'vision_region' && val('vision_scope') !== 'region') return null
      const disabled = runtimeControl(runtimeKey).disabled || id === 'vision' && runtimeKey !== 'vision_enabled' && !bool('vision_enabled')
      if (definition.type === 'boolean' && runtimeValue(runtimeKey) === undefined) {
        const field = fields.find(field => field.key === key)!
        return <StartupFieldRow key={key} field={{ ...field, editable: !disabled, value: undefined }} desktop={desktop} onSave={handleStartupSave} />
      }
      if (definition.type === 'boolean') return <SwitchCard key={key} icon={icon} {...runtimeControl(runtimeKey)} disabled={disabled}
        checked={bool(runtimeKey)} onChange={runtimeKey === 'vision_enabled' ? handleVisionEnabled : value => handleChange(runtimeKey, value)} />
      if (definition.options) return <ComboCard key={key} icon={icon} {...runtimeControl(runtimeKey)} disabled={disabled}
        value={val(runtimeKey)} onChange={value => handleChange(runtimeKey, ['integer', 'number'].includes(definition.type) ? Number(value) : value)} />
      const field = fields.find(field => field.key === key)!
      return <StartupFieldRow key={key} field={{ ...field, editable: !disabled, value: runtimeValue(runtimeKey) === undefined ? undefined : val(runtimeKey) }} desktop={desktop} onSave={handleStartupSave} />
    })
  }
  const visualPack = asRecord(config.visual_asset_pack)
  const visualPackInstalled = Boolean(visualPack.installed)
  const visualPackState = String(visualPack.state ?? 'not_installed')
  const visualPackValue = visualPackInstalled
    ? 'Installed'
    : visualPackState === 'incomplete' ? 'Incomplete' : visualPackState === 'invalid' ? 'Invalid package' : 'Not installed'
  const visualPackContent = visualPackInstalled
    ? 'Ambient layers, subtitles, scenario media, and wallpaper sound assets are available.'
    : visualPackState === 'incomplete' || visualPackState === 'invalid'
      ? `The optional visual pack needs attention: ${String(visualPack.message || (visualPack.missing as unknown[] || []).join(', ') || visualPackState)}`
      : 'Optional. The built-in wallpaper, Chat, Work, and headless mode remain available without it.'
  const characterPack = asRecord(config.character_pack)
  const characterPackInstalled = Boolean(characterPack.installed)
  const characterPackState = String(characterPack.state ?? 'not_installed')
  const characterPackValue = characterPackInstalled
    ? `Installed · ${Number(characterPack.clip_count ?? 0).toLocaleString()} clips`
    : characterPackState === 'invalid' ? 'Invalid package' : 'Not installed'
  const characterPackContent = characterPackInstalled
    ? `${Number(characterPack.frame_count ?? 0).toLocaleString()} indexed KTX2 frames · ${String(characterPack.relative_path ?? '')}`
    : characterPackState === 'invalid'
      ? `The optional package is incomplete: ${String(characterPack.message ?? 'validation failed')}`
      : 'Optional. Chat, Work, and headless mode remain available without this package.'
  const emotionPack = asRecord(config.emotion_reference_pack)
  const emotionPackInstalled = Boolean(emotionPack.installed)
  const emotionPackNeedsAttention = ['invalid', 'incomplete'].includes(String(emotionPack.state))
  const runtimePackages: RuntimePackageStatus[] = [
    {
      id: 'emotion_reference_pack',
      label: 'Kurisu V3 Emotion Reference Pack',
      description: 'Optional reference audio and paired transcripts. Existing V3 weights and the default acoustic reference are kept.',
      value: emotionPackInstalled ? 'Installed' : emotionPackNeedsAttention ? 'Needs attention' : 'Not installed',
      state: emotionPackInstalled ? 'ready' : emotionPackNeedsAttention ? 'attention' : 'inactive',
      stateLabel: emotionPackInstalled ? 'Installed' : emotionPackNeedsAttention ? 'Needs attention' : 'Not installed',
      icon: 'People',
    },
    {
      id: 'visual_runtime_pack',
      label: 'Visual Runtime Pack',
      description: visualPackContent,
      value: visualPackValue,
      state: visualPackInstalled ? 'ready' : ['incomplete', 'invalid'].includes(visualPackState) ? 'attention' : 'inactive',
      stateLabel: visualPackInstalled ? 'Installed' : ['incomplete', 'invalid'].includes(visualPackState) ? 'Needs attention' : 'Not installed',
      icon: 'Photo',
    },
    {
      id: 'character_pack',
      label: 'Kurisu Character Pack',
      description: characterPackContent,
      value: characterPackValue,
      state: characterPackInstalled ? 'ready' : characterPackState === 'invalid' ? 'attention' : 'inactive',
      stateLabel: characterPackInstalled ? 'Installed' : characterPackState === 'invalid' ? 'Needs attention' : 'Not installed',
      icon: 'People',
    },
    {
      id: 'vn_companion_portraits',
      label: 'VN Companion Portraits',
      description: companionPortraits?.detail || 'Reading the separately baked VN companion portrait assets.',
      value: companionPortraits && companionPortraits.frameCount > 0
        ? `${companionPortraits.emotionCount.toLocaleString()} emotions · ${companionPortraits.frameCount.toLocaleString()} frames`
        : companionPortraits?.state === 'invalid' || companionPortraits?.state === 'incomplete'
          ? 'Needs attention'
          : companionPortraits?.state === 'not_installed' ? 'Not installed' : 'Status unavailable',
      state: companionPortraits?.state === 'ready'
        ? 'ready'
        : companionPortraits?.state === 'invalid' || companionPortraits?.state === 'incomplete'
          ? 'attention'
          : companionPortraits?.state === 'not_installed' ? 'inactive' : 'unknown',
      stateLabel: companionPortraits?.state === 'ready'
        ? 'Installed'
        : companionPortraits?.state === 'invalid' || companionPortraits?.state === 'incomplete'
          ? 'Needs attention'
          : companionPortraits?.state === 'not_installed' ? 'Not installed' : 'Unavailable',
      icon: 'Movie',
    },
  ]

  const catalogDesktop = desktop ? { ...desktop, values: startupValues(desktop) } : null
  const mergeGroups = (bases: ConfigurationGroup[], running: ConfigurationGroup[]) => bases.map(base => {
    const backend = running.find(group => group.id === base.id)
    return { ...base, ...backend, fields: projectStartupFields(base.fields, backend?.fields, desktop) }
  })
  const modelConnections = asConfigurationGroups(config.model_connections)
  const backendProviderConfiguration = asConfigurationGroups(config.work_provider_configuration)
  const effectiveStartupValues = Object.fromEntries([...modelConnections, ...backendProviderConfiguration].flatMap(group =>
    group.fields.flatMap(field => field.key && field.type !== 'secret' && field.value !== undefined ? [[field.key, field.value]] : [])))
  const backendModelRoles = asConfigurationGroups(config.model_roles)
  const modelRoleCatalog = buildModelRoleCatalog(catalogDesktop)
  const modelRoles: ConfigurationGroup[] = [
    ...modelRoleCatalog.map(base => {
      const backend = backendModelRoles.find(group => group.id === base.id)
      return backend ? {
        ...backend,
        ...base,
        fields: projectStartupFields(base.fields, backend.fields, desktop),
        active: backend.active ?? base.active,
        configured: backend.configured ?? base.configured,
        status: backend.status || base.status,
        status_ok: backend.status_ok ?? base.status_ok,
        status_detail: backend.status_detail,
      } : { ...base, fields: projectStartupFields(base.fields, undefined, desktop) }
    }),
    ...backendModelRoles.filter(group => !modelRoleCatalog.some(base => base.id === group.id)),
  ]
  const mainModelProvider = val('llm_provider').trim().toLowerCase()
  const remoteModelConnectionIds = new Set(['deepseek', 'openai', 'gemini', 'bedrock'])
  const backendRemoteModelConnections = modelConnections.filter(group => remoteModelConnectionIds.has(group.id))
  const remoteModelConnections = mergeGroups(buildRemoteModelConnectionCatalog(mainModelProvider, catalogDesktop), backendRemoteModelConnections)
  const backendLocalModelConnections = modelConnections.filter(group => ['local', 'hybrid_local'].includes(group.id))
  const localModelConnections = mergeGroups(buildLocalModelConnectionCatalog(mainModelProvider, catalogDesktop, effectiveStartupValues), backendLocalModelConnections)
  const backendOptionalModelConnections = modelConnections.filter(group => group.id === 'character_rag')
  const optionalModelConnections = mergeGroups(buildOptionalModelServiceCatalog(catalogDesktop), backendOptionalModelConnections)
  const modelProviderLabels: Record<string, string> = Object.fromEntries(desktopCatalogFields.LLM_PROVIDER.options!.map(option =>
    typeof option === 'string' ? [option, option] : [option.value, option.label['en-US']]))
  const modelGroupReady = (id: string) => {
    const group = modelConnections.find(item => item.id === id)
    return Boolean(group && (typeof group.status_ok === 'boolean' ? group.status_ok : group.configured))
  }
  const mainModelReady = ({
    hybrid: ['hybrid_local', 'bedrock'],
    hybrid2: ['hybrid_local', 'deepseek'],
    hybrid3: ['hybrid_local', 'openai'],
  }[mainModelProvider] || [mainModelProvider]).every(modelGroupReady)
  const mainModelLabel = modelProviderLabels[mainModelProvider] || mainModelProvider
  const imageModel = mainModelProvider === 'deepseek' || mainModelProvider === 'hybrid2'
    ? String(modelConnections.find(group => group.id === 'deepseek')?.fields
      ?.find(field => field.key === 'DEEPSEEK_MODEL_NAME')?.value || 'deepseek-v4-flash')
    : ''
  const visionModelReady = mainModelReady && chatProviderSupportsImages(mainModelProvider, imageModel)
  const configuredTranslationModels = modelConnections
    .filter(group => ['deepseek', 'openai', 'gemini'].includes(group.id) && (group.status_ok ?? group.configured))
    .map(group => group.label || modelProviderLabels[group.id] || group.id)
  const providerCatalog = buildWorkProviderCatalog({
    provider: connected ? String(config.cooperative_chat_provider ?? '') : '',
    codingProvider: connected ? String(config.work_coding_provider ?? '') : '',
    roleCandidates: providerRoleCandidates,
  }, catalogDesktop, effectiveStartupValues)
  const selectedWorkProvider = String(providerCatalog.routing.fields.find(field => field.key === 'WORK_EXECUTION_PROVIDER')?.value ?? '').toLowerCase()
  const selectedCodingProvider = String(providerCatalog.routing.fields.find(field => field.key === 'WORK_CODING_PROVIDER')?.value ?? '').toLowerCase()
  const workProviderLabels: Record<string, string> = { codex: 'Codex agent', openclaw: 'OpenClaw agent', browser: 'Browser provider', pi: 'Pi daily agent' }
  const workProviderAssignment = `${t('Coding')}: ${workProviderLabels[selectedCodingProvider] || selectedCodingProvider || t('Unknown')} · ${t('Everyday execution')}: ${workProviderLabels[selectedWorkProvider] || selectedWorkProvider || t('Unknown')}`
  const roleGroups = Object.fromEntries(modelRoles.map(group => [group.id, group])) as Record<string, ConfigurationGroup>
  const advancedRoleIds = [
    'work_planner', 'work_observer', 'browser_branch_planner', 'auip_narration',
    'auip_action', 'vn_subtitle_translation', 'vn_speech_translation',
  ]
  const advancedOverrideCount = advancedRoleIds.filter(id =>
    roleGroups[id]?.status === 'override'
      || (roleGroups[id]?.fields || []).some(item => item.key && desktop?.sources?.[item.key] === 'user'),
  ).length
  const graphicsRuntime = connected ? config.graphics as GraphicsRuntimeSettings | undefined : undefined
  const graphicsConfiguration = buildGraphicsConfiguration(graphicsRuntime, catalogDesktop)
  const providerConfiguration: ConfigurationGroup[] = providerCatalog.connections.map(base => {
    const backend = backendProviderConfiguration.find(group => group.id === base.id)
    return backend ? {
      ...backend,
      ...base,
      fields: projectStartupFields(base.fields, backend.fields, desktop),
      active: base.active,
      configured: backend.configured ?? base.configured,
      status: backend.status || base.status,
      status_ok: backend.status_ok ?? base.status_ok,
      status_detail: backend.status_detail,
    } : { ...base, fields: projectStartupFields(base.fields, undefined, desktop) }
  })
  const artifactConfiguration = mergeGroups([catalogConfiguration("auip_artifact_style", catalogDesktop)], asConfigurationGroups(config.artifact_configuration))
  const backendVoiceConfiguration = asConfigurationGroups(config.voice_configuration)
  const voiceCatalog = buildVoiceConfigurationCatalog({
        asrBackend: desktop?.sources?.ASR_BACKEND === 'user' ? desktop.values.ASR_BACKEND : val('asr_backend', 'qwen3_asr'),
        ttsBackend: desktop?.sources?.TTS_BACKEND === 'user' ? desktop.values.TTS_BACKEND : val('tts_backend', 'gpt_sovits'),
        wakeEnabled: desktop?.sources?.WAKE_ENABLED === 'user' ? desktop.values.WAKE_ENABLED === 'true' : bool('wake_enabled'),
        aecEnabled: desktop?.sources?.AEC_REALTIME_ENABLED === 'user' ? desktop.values.AEC_REALTIME_ENABLED === 'true' : config.aec_realtime_enabled === undefined ? Boolean(desktopCatalogFields.AEC_REALTIME_ENABLED.default) : bool('aec_realtime_enabled'),
        emotionReferencesEnabled: backendVoiceConfiguration.find(group => group.id === 'tts_emotion_references')?.fields.find(field => field.key === 'ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING')?.value === true,
      }, catalogDesktop)
  const voiceConfiguration: ConfigurationGroup[] = voiceCatalog.map(base => {
    const backend = backendVoiceConfiguration.find(group => group.id === base.id)
    return backend ? {
      ...backend,
      ...base,
      fields: projectStartupFields(base.fields, backend.fields, desktop),
      configured: backend.configured ?? base.configured,
      status: backend.status || base.status,
      status_ok: backend.status_ok ?? base.status_ok,
      status_detail: backend.status_detail,
    } : { ...base, fields: projectStartupFields(base.fields, undefined, desktop) }
  })
  // Keep complete voice groups together; voice files and engine settings share one editor.
  const { output: outputVoiceIds, input: inputVoiceIds, remote: remoteVoiceIds } = voiceConfigurationSections
  const outputVoiceConfiguration = voiceConfiguration.filter(group => outputVoiceIds.has(group.id))
  const inputVoiceConfiguration = voiceConfiguration.filter(group => inputVoiceIds.has(group.id))
  const remoteVoiceConfiguration = voiceConfiguration.filter(group => remoteVoiceIds.has(group.id))
  const advancedVoiceConfiguration = voiceConfiguration.filter(group => !outputVoiceIds.has(group.id) && !inputVoiceIds.has(group.id) && !remoteVoiceIds.has(group.id))
  const speechBackend = voiceConfiguration.find(group => group.id === 'speech_synthesis')?.fields.find(field => field.key === 'TTS_BACKEND')
  const speechBackendOption = speechBackend?.options?.find(option => typeof option !== 'string' && option.value === speechBackend.value)
  const voiceSummary = typeof speechBackendOption === 'object' ? speechBackendOption.label : String(speechBackend?.value || '')
  const visionWindowHandle = val('vision_window_handle')
  const visionWindowOptions: ComboOption[] = [
    { value: '', label: 'Select a window…' },
    ...visionWindows.map(item => ({
      value: item.hwnd,
      label: item.processName ? `${item.title} · ${item.processName}` : item.title,
    })),
  ]
  if (visionWindowHandle && !visionWindowOptions.some(option => typeof option !== 'string' && option.value === visionWindowHandle)) {
    visionWindowOptions.splice(1, 0, { value: visionWindowHandle, label: 'Previously selected window · unavailable' })
  }
  const avatarConfiguration = mergeGroups([catalogConfiguration("vts_compatibility", catalogDesktop)], asConfigurationGroups(config.avatar_configuration))
  const capabilityProfiles = useMemo(
    () => buildCapabilityProfiles(connected ? config : {}, connected ? providerAvailability : []),
    [connected, config, providerAvailability],
  )
  const sharedCapabilities = useMemo(() => capabilityPackages.flatMap(packageInfo =>
    (packageInfo.contributions || [])
      .filter(contribution => contribution.kind === 'skill'
        || (contribution.kind === 'mcp_server' && packageInfo.source !== 'desktop:mcp-registry'))
      .map(contribution => ({ packageInfo, contribution })),
  ), [capabilityPackages])

  const capabilityConsumers = useCallback((contribution: CapabilityContribution): string[] => {
    const projections = new Set((contribution.bindings || []).filter(binding => binding.enabled).map(binding => binding.projection))
    const selectedProviderIds = Array.isArray(contribution.metadata?.provider_ids)
      ? new Set((contribution.metadata.provider_ids as unknown[]).map(value => String(value)))
      : null
    const consumers = providerManifests
      .filter(manifest => (!selectedProviderIds || selectedProviderIds.has(manifest.provider_id))
        && (manifest.capabilities?.capability_projections || []).some(projection => projections.has(projection)))
      .map(manifest => manifest.display_name || manifest.provider_id)
    if (contribution.kind === 'mcp_server') {
      const ownProvider = String(contribution.metadata?.provider_id || '')
      const manifest = providerManifests.find(item => item.provider_id === ownProvider)
      if (manifest) consumers.push(manifest.display_name || manifest.provider_id)
    }
    return [...new Set(consumers)]
  }, [providerManifests])

  const moveModelTab = (event: ReactKeyboardEvent<HTMLButtonElement>, current: ModelsPage) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    const next: ModelsPage = current === 'roles' ? 'connections' : 'roles'
    setModelsPage(next)
    document.getElementById(`models-tab-${next}`)?.focus()
  }

  const openCapabilityTarget = useCallback((targetSection: SceneConfigureSection, targetId?: string) => {
    if (targetId === 'character_rag') { openCharacters('knowledge'); return }
    setSection(targetSection)
    let anchor = ''
    if (targetSection === 'models') {
      if (targetId === 'application_interaction' || targetId?.startsWith('auip_')) {
        setModelsPage('roles')
        setAdvancedRolesOpen(true)
        anchor = 'settings-auip-pipeline'
      } else {
        setModelsPage('connections')
        anchor = 'models-panel-connections'
      }
    } else if (targetSection === 'general') {
      anchor = targetId?.includes('visual') ? 'settings-vision' : targetId?.includes('translation') ? 'settings-language' : ''
    }
    if (anchor) window.setTimeout(() => document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
  }, [openCharacters])

  const characterWorkspace = <CharacterPage connected={connected} section={characterTab} onSectionChange={setCharacterTab}
    voiceSummary={(connected ? config.tts_backend : desktop) ? voiceSummary : ''} onOpenVoice={() => setSection('voice')}
    panels={{
      identity: <>
                <SettingsGroup title="Character roles" detail="Create user roles and choose the role for the next backend start.">
                  <CharacterManagementSettings send={send} connected={connected} desktop={desktop}
                    restarting={restarting}
                    kurisuPreview={config.main_chat_character_prompt_preview}
                    onSettingsChanged={settings => setDesktop(settings as unknown as DesktopSettingsSnapshot)} />
                </SettingsGroup>
        <details className="character-persona-details"><summary>{t('Kurisu Japanese persona')}</summary><div>
                <SettingsGroup title="Kurisu Japanese persona" detail="Customize Kurisu’s Japanese personality for later Main Chat and AUIP/browser decisions and speech. Choices stay within the application’s rules.">
                  <MainChatCharacterSettings
                    savedOverride={val('main_chat_character_prompt_ja')}
                    preview={config.main_chat_character_prompt_preview}
                    canSave={connected || Boolean(desktop)}
                    locked={Boolean(desktop?.locked?.AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA)}
                    saving={saving === 'main_chat_character_prompt_ja'}
                    onSave={value => handleChange('main_chat_character_prompt_ja', value)}
                  />
                </SettingsGroup>
        </div></details>
      </>,
      appearance: <>
        <CharacterVisualsPage send={send} subscribe={subscribe} connected={connected} />
                <SettingsGroup title="Chat appearance" detail="Local presentation only; avatar images are never sent to the model.">
                  <ChatAvatarSettings />
                </SettingsGroup>
        {connected ? <SettingsGroup title="Installed visual resources">
          <RuntimePackages runtimePackages={runtimePackages.filter(item => ['visual_runtime_pack', 'character_pack', 'vn_companion_portraits'].includes(item.id))} />
        </SettingsGroup> : null}
      </>,
      knowledge: <>
        <div className="character-history-grid">
          <article><h4>{t('Conversations stay with their role')}</h4><p>{t('Each role keeps its own conversation history. Restart with the owning role to continue or manage its chats.')}</p></article>
          <article><h4>{t('Projects and Work remain shared')}</h4><p>{t('Retained projects, tasks and deliverables keep their existing sharing rules. Switching roles does not move or rewrite them.')}</p></article>
        </div>
        <SettingsGroup title="Kurisu reference library" detail="Uses the existing built-in Kurisu reference index. Other roles do not inherit this library.">
          {optionalModelConnections.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible defaultOpen />)}
        </SettingsGroup>
      </>,
    }} />

  return (
    <div className="settings-scroll-area flex-1 overflow-y-auto">
      <div style={{ width: section === 'characters' && characterTab === 'appearance' ? '100%' : 'min(100%, 1010px)', padding: '20px 24px 32px' }}>
        <div className="flex items-center justify-between gap-4" style={{ marginBottom: 16 }}>
          <div>
            <h2 className="settings-page-title">{t('Settings')}</h2>
            <div className="settings-page-context">
              {t(section === 'capabilities' ? 'Shared capabilities, implementations, and scene use.'
                : 'Runtime controls and desktop connection profiles.')}
            </div>
          </div>
          {restartPending || (section === 'characters' && desktop) ? (
            <button onClick={() => void restartBackend()} disabled={restarting} className="text-[11px] font-[600] rounded-md disabled:opacity-50" style={{ height: 32, padding: '0 12px', whiteSpace: 'nowrap', flexShrink: 0, color: 'white', background: 'var(--accent)', border: 0 }}>
              {t(restarting ? 'Restarting…' : restartPending ? 'Restart backend to apply' : 'Restart backend')}
            </button>
          ) : null}
        </div>

        {(saving || notice || error) ? (
          <div className="settings-feedback" aria-live="polite">
            {saving ? <div className="text-[11px] animate-pulse" style={{ color: 'var(--accent)' }}>{t('Saving {name}…', { name: saving })}</div> : null}
            {notice ? <div className="text-[11px] rounded-md p-3" style={{ color: 'var(--warning)', background: 'var(--warning-bg)' }}>{t(notice)}</div> : null}
            {error ? <div role="alert" className="text-[11px] rounded-md p-3" style={{ color: 'var(--danger)', background: 'var(--danger-bg)' }}>{error}</div> : null}
          </div>
        ) : null}

        <BackendStartupRecovery connected={connected} restarting={restarting}
          reconnectBackend={reconnectBackend} onSettingsChanged={settings => setDesktop(settings as unknown as DesktopSettingsSnapshot)} />

        <RetiredRouteSetting migration={retiredRouteMigration(desktop, config.retired_settings)}
          saving={saving === RETIRED_ROUTE_KEY} onConfirm={confirmRetiredRoute} />

        <div className="settings-layout flex gap-6 items-start" style={{ width: '100%' }}>
          <nav className="settings-section-nav shrink-0 flex flex-col gap-0.5 sticky" style={{ width: 138, top: 16 }} aria-label={t('Settings sections')}>
            {([
              ['capabilities', 'Capabilities', 'Tiles'],
              ['general', 'General', 'Setting'],
              ['characters', 'Characters', 'People'],
              ['graphics', 'Graphics', 'Video'],
              ['models', 'Models', 'Robot'],
              ['voice', 'Voice', 'Microphone'],
              ['providers', 'Providers', 'Work'],
            ] as Array<[SettingsSection, string, FluentIconName]>).map(([id, label, icon]) => (
              <button key={id} onClick={() => setSection(id)} aria-current={section === id ? 'page' : undefined} className="flex items-center gap-2 text-[11.5px] text-left rounded-md px-2.5" style={{ height: 32, color: section === id ? 'var(--text)' : 'var(--muted)', background: section === id ? 'var(--selected-fill)' : 'transparent', border: 0, fontWeight: section === id ? 650 : 500 }}>
                <FluentIcon name={icon} size={14} />{t(label)}
              </button>
            ))}
          </nav>

          <main className="settings-main flex-1 min-w-0" style={{ maxWidth: section === 'characters' && characterTab === 'appearance' ? undefined : 760 }}>
            {section === 'characters' ? characterWorkspace : null}
            {section === 'capabilities' ? (
              <CapabilitiesPanel capabilities={capabilityProfiles} runtimePackages={runtimePackages} onOpenSection={openCapabilityTarget} />
            ) : null}

            {section === 'graphics' ? (
              <div className="flex flex-col gap-5">
                <BoundaryNote title="Graphics">
                  {t('Applies to character rendering and wallpapers, not model inference or voice processing. Restart the backend after saving, then reopen existing character and wallpaper windows.')}
                </BoundaryNote>
                {graphicsRuntime ? <BoundaryNote title="Current backend limits">
                  {graphicsRuntime.effective_max_fps} FPS · {graphicsRuntime.effective_max_resolution === null
                    ? t('Native pixel density') : `${graphicsRuntime.effective_max_resolution}× ${t('pixel density')}`}
                  <div>{t('Wallpaper Engine can impose a lower FPS limit. These are configured ceilings, not measured performance.')}</div>
                </BoundaryNote> : <BoundaryNote title="Backend status unavailable">
                  {t('You can save graphics settings while disconnected. Current renderer limits will appear when the backend connects.')}
                </BoundaryNote>}
                {graphicsConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} icon="Video" collapsible={group.id === 'graphics_sampling'} />)}
              </div>
            ) : null}

            {section === 'general' ? (
              <div className="flex flex-col gap-5">
                {desktop?.platform === 'win32' ? (
                  <SettingsGroup title={startupConfiguration.label} detail={startupConfiguration.description}>
                    {startupConfiguration.fields.map(field => field.options ? <ComboCard key={field.key} icon="Setting" title={field.label}
                      content={desktop.locked?.[field.key] ? 'Startup mode is controlled by your launch environment.' : field.description || ''}
                      value={String(field.value ?? '')} disabled={saving === field.key || desktop.locked?.[field.key]}
                      options={field.value === undefined ? [{ value: '', label: 'Unknown' }, ...field.options] : field.options} onChange={value => {
                        void handleStartupSave(field, value, false)
                          .then(() => setNotice('Startup mode saved. Quit and reopen Amadeus to apply.'))
                          .catch(() => { /* handleStartupSave displays the save error. */ })
                      }} /> : <StartupFieldRow key={field.key} field={field} desktop={desktop} onSave={handleStartupSave} />)}
                  </SettingsGroup>
                ) : null}
                <SettingsGroup title="Appearance" detail="Choose the visual style used across the Electron frontend.">
                  <ThemePicker
                    value={theme}
                    disabled={Boolean(desktop?.locked?.AMADEUS_UI_THEME)}
                    onChange={value => void setTheme(value).catch(reason => setError(reason instanceof Error ? reason.message : 'Could not save interface theme'))}
                  />
                </SettingsGroup>
                <SettingsGroup title="Characters" detail="Manage personalities, artwork, voice and reference knowledge together.">
                  <button className="character-secondary-button" onClick={() => openCharacters('overview')}>{t('Open Characters')}</button>
                </SettingsGroup>
                <div id="settings-language"><SettingsGroup title={catalogGroups.find(group => group.id === 'presentation')!.title['en-US']} detail={catalogGroups.find(group => group.id === 'presentation')!.description['en-US']}>
                  {interfaceConfiguration.fields.filter(field => field.key !== 'AMADEUS_UI_THEME').map(field => field.key === 'AMADEUS_UI_LOCALE'
                    ? <ComboCard key={field.key} icon="Language" title={field.label} content={field.description || ''} value={locale}
                        disabled={Boolean(desktop?.locked?.[field.key])}
                        onChange={value => void setLocale(value as UiLocale).catch(reason => setError(reason instanceof Error ? reason.message : 'Could not save console language'))}
                        options={field.options || []} />
                    : <StartupFieldRow key={field.key} field={field} desktop={desktop} onSave={handleStartupSave} />)}
                  {renderRuntimeGroup('presentation', 'Language')}
                </SettingsGroup></div>
                <div id="settings-vision"><SettingsGroup title={catalogGroups.find(group => group.id === 'vision')!.title['en-US']} detail={catalogGroups.find(group => group.id === 'vision')!.description['en-US']}>
                  {renderRuntimeGroup('vision', 'Camera')}
                  {bool('vision_enabled') && val('vision_scope', 'full_screen') === 'selected_window' ? (
                    visionWindows.length || visionWindowHandle ? (
                      <ComboCard
                        icon="Tiles"
                        title={runtimeControl('vision_window_handle').title}
                        content={runtimeControl('vision_window_handle').content}
                        value={visionWindowHandle}
                        onChange={value => { if (value) void handleVisionWindowTarget(value) }}
                        options={visionWindowOptions}
                        disabled={visionWindowsLoading}
                      />
                    ) : (
                      <BoundaryNote title="Vision target required">
                        <span>{t(visionWindowsLoading ? 'Finding open windows…' : 'No selectable window is available. Open the target window, then refresh the list.')}</span>
                        {!visionWindowsLoading ? (
                          <button type="button" className="settings-inline-link" onClick={() => void loadVisionWindows()}>{t('Refresh windows')}</button>
                        ) : null}
                      </BoundaryNote>
                    )
                  ) : null}
                </SettingsGroup></div>
                <SettingsGroup title="Avatar compatibility" detail="Optional output paths are disabled unless explicitly enabled.">
                  {avatarConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} />)}
                </SettingsGroup>
              </div>
            ) : null}

            {section === 'models' ? (
              <div className="flex flex-col gap-4">
                <div className="capability-page-tabs" role="tablist" aria-label={t('Model settings views')}>
                  <button id="models-tab-roles" aria-controls="models-panel-roles" type="button" role="tab" aria-selected={modelsPage === 'roles'} tabIndex={modelsPage === 'roles' ? 0 : -1} onKeyDown={event => moveModelTab(event, 'roles')} onClick={() => setModelsPage('roles')}>{t('Role assignments')}</button>
                  <button id="models-tab-connections" aria-controls="models-panel-connections" type="button" role="tab" aria-selected={modelsPage === 'connections'} tabIndex={modelsPage === 'connections' ? 0 : -1} onKeyDown={event => moveModelTab(event, 'connections')} onClick={() => setModelsPage('connections')}>{t('Model connections')}</button>
                </div>

                <div role="tabpanel" id={`models-panel-${modelsPage}`} aria-labelledby={`models-tab-${modelsPage}`}>
                {modelsPage === 'roles' ? (
                  <div className="flex flex-col gap-5">
                    <SettingsGroup title="Conversation roles" detail="Each role may inherit a shared model or declare an explicit override when the runtime supports it.">
                      <ComboCard icon="Robot" {...runtimeControl('llm_provider')} value={val('llm_provider')} onChange={value => void handleMainProviderChange(value)} />
                      <RoleAssignmentCard
                        icon="Camera"
                        title="Visual understanding"
                        description="Uses direct image input from the main conversation model."
                        assignment={`${mainModelLabel} · ${t('Inherited')}`}
                        policy="Inherits Main conversation"
                        status={visionModelReady ? 'Available' : mainModelReady ? 'Needs multimodal model' : 'Needs setup'}
                        statusOk={visionModelReady}
                        onConfigure={() => setModelsPage('connections')}
                      />
                      <ConfigurationCard group={roleGroups.vn_companion} desktop={desktop} onSave={handleStartupSave} collapsible />
                    </SettingsGroup>

                    <SettingsGroup title="Work & application roles" detail="Assign coding and everyday execution separately. Each Provider keeps its own model and tools; connections and registration are managed in Providers.">
                      <ConfigurationCard group={providerCatalog.routing} desktop={desktop} onSave={handleStartupSave} />
                    </SettingsGroup>

                    <SettingsGroup title="Presentation roles">
                      <RoleAssignmentCard
                        icon="Language"
                        title="Presentation translation"
                        description="Shared translation used by Chat, Wallpaper, and VN presentation policies."
                        assignment={configuredTranslationModels.length ? `${t('Automatic')} · ${configuredTranslationModels.join(', ')}` : 'Automatic · No configured model'}
                        policy="Automatic selection"
                        status={configuredTranslationModels.length ? 'Available' : 'Needs setup'}
                        statusOk={configuredTranslationModels.length > 0}
                        onConfigure={() => setModelsPage('connections')}
                      />
                    </SettingsGroup>

                    <details className="model-advanced-roles" open={advancedRolesOpen} onToggle={event => setAdvancedRolesOpen(event.currentTarget.open)}>
                      <summary>
                        <span>{t('Advanced role overrides')}</span>
                        <small>{advancedOverrideCount} {t('overridden')} · {advancedRoleIds.length - advancedOverrideCount} {t('inherited')}</small>
                      </summary>
                      <div className="flex flex-col gap-5">
                        <SettingsGroup title="Routing & observation">
                          <ConfigurationCard group={roleGroups.work_planner} desktop={desktop} onSave={handleStartupSave} collapsible />
                          <ConfigurationCard group={roleGroups.work_observer} desktop={desktop} onSave={handleStartupSave} collapsible />
                          <ConfigurationCard group={roleGroups.browser_branch_planner} desktop={desktop} onSave={handleStartupSave} collapsible />
                        </SettingsGroup>

                        <div id="settings-auip-pipeline"><SettingsGroup title="AUIP pipeline">
                          <ConfigurationCard group={roleGroups.auip_action} desktop={desktop} onSave={handleStartupSave} collapsible />
                          <ConfigurationCard group={roleGroups.auip_narration} desktop={desktop} onSave={handleStartupSave} collapsible />
                        </SettingsGroup></div>

                        <SettingsGroup title="VN translation pipeline">
                          <ConfigurationCard group={roleGroups.vn_subtitle_translation} desktop={desktop} onSave={handleStartupSave} collapsible />
                          <ConfigurationCard group={roleGroups.vn_speech_translation} desktop={desktop} onSave={handleStartupSave} collapsible />
                        </SettingsGroup>

                        <BoundaryNote title="Internal runtime roles">
                          AUIP authorizer and participant stages share AUIP action decision. Provider-owned subagents and transient classifiers remain diagnostic details rather than independent model settings.
                        </BoundaryNote>
                      </div>
                    </details>
                  </div>
                ) : (
                  <div className="flex flex-col gap-5">
                    <SettingsGroup title="Remote model services" detail="Add credentials here before assigning a service to a role. Connections remain visible even when the backend is offline.">
                      {remoteModelConnections.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible defaultOpen={Boolean(group.active)} optionalWhenInactive />)}
                    </SettingsGroup>
                    <SettingsGroup title="Local model runtimes" detail="Local and hybrid endpoints are configured independently from remote API credentials.">
                      {localModelConnections.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible defaultOpen={Boolean(group.active)} optionalWhenInactive />)}
                    </SettingsGroup>
                    <SettingsGroup title="Character knowledge" detail="Reference knowledge and history boundaries are managed in Characters.">
                      <button className="character-secondary-button" onClick={() => openCharacters('knowledge')}>{t('Open character knowledge')}</button>
                    </SettingsGroup>
                  </div>
                )}
                </div>
              </div>
            ) : null}

            {section === 'voice' ? (
              <div className="flex flex-col gap-5">
                <BoundaryNote title="Voice data boundary">
                  Wake and Conversation recognition are independent roles. Selecting a remote backend sends confirmed conversation audio or synthesis text to the configured endpoint; Amadeus never silently falls back from local to remote.
                </BoundaryNote>
                <BoundaryNote title="Shared voice setup">
                  {t('All roles use this voice setup. Model weights, voice profiles, reference audio and speech services are configured here. Changing personality does not change the voice.')}
                </BoundaryNote>
                <SettingsGroup title="Speech output" detail="Choose a synthesis engine, then its model weights and reference audio. Save and restart the backend to apply startup settings.">
                  {outputVoiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible defaultOpen={group.id === 'speech_synthesis'} optionalWhenInactive />)}
                </SettingsGroup>
                <SettingsGroup title="Speech language" detail="User-facing language for generated speech.">
                  <ComboCard icon="Language" title={desktopCatalogFields.TTS_OUTPUT_LANGUAGE.title['en-US']} content={desktopCatalogFields.TTS_OUTPUT_LANGUAGE.description?.['en-US'] || ''} value={val('tts_output_language', 'ja')} onChange={value => handleChange('tts_output_language', value)} options={desktopCatalogFields.TTS_OUTPUT_LANGUAGE.options!.map(option => ({ value: String(runtimeSettingFromDesktopValues('tts_output_language', { TTS_OUTPUT_LANGUAGE: typeof option === 'string' ? option : option.value })), label: typeof option === 'string' ? option : option.label['en-US'] }))} />
                </SettingsGroup>
                <SettingsGroup title="Listening & recognition" detail="Configure conversation transcription, wake listening and microphone processing.">
                  {inputVoiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible optionalWhenInactive />)}
                </SettingsGroup>
                <SettingsGroup title="Remote voice services" detail="Configure credentials and endpoints for remote transcription and speech synthesis.">
                  {remoteVoiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible defaultOpen={Boolean(group.active)} optionalWhenInactive />)}
                </SettingsGroup>
                {connected ? <SettingsGroup title="Installed voice resources">
                  <RuntimePackages runtimePackages={runtimePackages.filter(item => item.id === 'emotion_reference_pack')} />
                </SettingsGroup> : <BoundaryNote title="Backend status unavailable">{t('Connect the backend to inspect installed voice resources. Saved voice selections remain editable.')}</BoundaryNote>}
                <details className="model-advanced-roles">
                  <summary>
                    <span>{t('Advanced voice settings')}</span>
                    <small>{t('Engine paths, performance, and other services')}</small>
                  </summary>
                  <div className="flex flex-col gap-5">
                    <SettingsGroup title="Local speech performance" detail="Performance tuning for the embedded GPT-SoVITS engine.">
                      <ComboCard icon="Tiles" title="Local TTS inference mode" content="Choose standard single synthesis, CUDA Graph, or explicit parallel generation while speech is idle." value={val('tts_mode', 'parallel')} onChange={value => handleChange('tts_mode', value)} options={[{ value: 'parallel', label: 'Standard ×1' }, { value: 'cuda_graph', label: 'CUDA Graph ×1' }, { value: 'parallel2', label: 'Parallel ×2' }]} disabled={val('tts_backend', 'gpt_sovits') !== 'gpt_sovits'} />
                    </SettingsGroup>
                    {advancedVoiceConfiguration.length > 0 ? <SettingsGroup title="Voice implementation details" detail="Engine paths, performance and additional voice services.">
                      {advancedVoiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible optionalWhenInactive />)}
                    </SettingsGroup> : null}
                  </div>
                </details>
              </div>
            ) : null}

            {section === 'providers' ? (
              <div className="flex flex-col gap-5">
                <BoundaryNote title="Execution boundary">
                  Main Chat may delegate work to a Provider. Skills and MCP connections are shared only among compatible Work Providers; their prompts and tool schemas are never attached directly to Main Chat.
                </BoundaryNote>
                <BoundaryNote title="Work role assignments">
                  <span>{workProviderAssignment}</span>{' '}
                  <button className="settings-action" onClick={() => { setSection('models'); setModelsPage('roles') }}>{t('Configure roles')}</button>
                </BoundaryNote>
                <SettingsGroup title="Work Provider connections" detail="Registered means the adapter passed its startup boundary. Remote availability is verified when that Provider connects.">
                  {providerConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} availability={providerAvailability.find(item => item.provider_id === group.id)} onSave={handleStartupSave} collapsible optionalWhenInactive />)}
                </SettingsGroup>
                <SettingsGroup title="ACP agents — Experimental" detail="Try DeepSeek Harness, Claude or another ACP v1 agent. This integration has not been used in production.">
                  <AcpProviders
                    encoded={desktop?.sources?.AMADEUS_ACP_PROVIDERS === 'user' ? desktop.values.AMADEUS_ACP_PROVIDERS : acpAgents}
                    locked={!desktop || Boolean(desktop.locked?.AMADEUS_ACP_PROVIDERS)}
                    electronUnavailable={!desktop}
                    configurations={acpConfigurations}
                    onSave={async value => {
                      await handleStartupSave({ key: 'AMADEUS_ACP_PROVIDERS', label: 'ACP agents', type: 'text', editable: true, restart_required: true }, value, false)
                    }}
                    onRefresh={async () => {
                      const response = await send('provider.list', {})
                      setAcpConfigurations((response.provider_configurations || []) as AcpConfiguration[])
                    }}
                  />
                  {(Array.isArray(config.acp_credentials) ? config.acp_credentials as StartupField[] : []).map(field =>
                    <StartupFieldRow key={field.key} field={field} desktop={desktop} onSave={handleStartupSave}/>)}
                </SettingsGroup>
                <SettingsGroup title="Artifact appearance" detail="Generation preferences for new interactive apps. Save and restart the backend to apply.">
                  {artifactConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} />)}
                </SettingsGroup>
                <SettingsGroup title="MCP connections" detail="Host-managed connections are projected only into explicitly compatible Work Providers.">
                  <McpConnections
                    connections={desktop?.mcpConnections || []}
                    locked={Boolean(desktop?.mcpConnectionsLocked)}
                    providers={providerManifests}
                    restartPending={restartPending}
                    send={send}
                    onSettingsChanged={settings => setDesktop(settings as unknown as DesktopSettingsSnapshot)}
                    onRestartRequired={() => setRestartPending(true)}
                  />
                </SettingsGroup>
                <SettingsGroup title="Shared Provider capabilities" detail="Installed once by the Host, then projected only to Providers that explicitly support the capability shape.">
                  {sharedCapabilities.map(({ packageInfo, contribution }) => <CapabilityCard key={`${packageInfo.id}-${contribution.kind}-${contribution.id}`} contribution={contribution} packageInfo={packageInfo} consumers={capabilityConsumers(contribution)} />)}
                  {!sharedCapabilities.length ? <div className="text-[10.5px]" style={{ color: 'var(--muted)' }}>{t('No shared Provider capability is active in this backend process.')}</div> : null}
                </SettingsGroup>
              </div>
            ) : null}

          </main>
        </div>
      </div>
    </div>
  )
}
