import { DEFAULT_WINDOWS_STARTUP_MODE } from '../../main/startupMode'
import { useState, useEffect, useCallback, useMemo, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import McpConnections, { type McpConnectionSummary } from './McpConnections'
import ChatAvatarSettings from './ChatAvatarSettings'
import AcpProviders, { type AcpConfiguration } from './AcpProviders'
import CapabilitiesPanel, { type RuntimePackageStatus } from './CapabilitiesPanel'
import { buildCapabilityProfiles, type SceneConfigureSection } from './sceneCapabilityProjection'
import { useI18n, type UiLocale } from '../i18n'
import { useTheme, type UiTheme } from '../theme'
import { chatProviderSupportsImages } from './chatModelCapabilities'
import {
  buildLocalModelConnectionCatalog,
  buildOptionalModelServiceCatalog,
  buildRemoteModelConnectionCatalog,
} from './modelConnectionCatalog'
import { buildVoiceConfigurationCatalog } from './voiceConfigurationCatalog'
import { buildWorkProviderCatalog } from './providerConnectionCatalog'
import { buildModelRoleCatalog } from './modelRoleCatalog'
import { buildGraphicsConfiguration, type GraphicsRuntimeSettings } from './graphicsConfigurationCatalog'
import { markRuntimeSettingsApplied, persistDesktopRuntimeSettings, runtimeSettingFromDesktopValues } from './desktopRuntimeSettings'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
  reconnectBackend: () => Promise<void>
}

type SettingsSection = 'capabilities' | 'graphics' | SceneConfigureSection
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

function GroupTitle({ children, detail }: { children: string; detail?: string }) {
  const { t } = useI18n()
  return (
    <div>
      <h3 className="settings-group-title">{t(children)}</h3>
      {detail ? <div className="settings-group-description">{t(detail)}</div> : null}
    </div>
  )
}

function CardShell({ children, vertical = false }: { children: ReactNode; vertical?: boolean }) {
  return (
    <div
      className={`setting-card flex ${vertical ? 'flex-col items-stretch' : 'items-center'}`}
      style={{
        minHeight: vertical ? undefined : 64,
        backgroundColor: 'var(--surface)',
        border: '1px solid var(--card-border)',
        borderRadius: 11,
        padding: 12,
        boxShadow: '0 1px 2px color-mix(in srgb, var(--shadow-color) 25%, transparent)',
      }}
    >
      {children}
    </div>
  )
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
        {options.map(option => {
          const optionValue = typeof option === 'string' ? option : option.value
          const label = typeof option === 'string' ? option : option.label
          return <option key={optionValue} value={optionValue}>{t(label)}</option>
        })}
      </select>
    </CardShell>
  )
}

function CardIcon({ name }: { name: FluentIconName }) {
  return (
    <>
      <div className="shrink-0 flex items-center justify-center" style={{ width: 24, color: 'var(--muted)' }}>
        <FluentIcon name={name} size={17} />
      </div>
      <div style={{ width: 11, flexShrink: 0 }} />
    </>
  )
}

function SwitchCard({ icon, title, content, checked, onChange }: {
  icon: FluentIconName; title: string; content: string; checked: boolean; onChange: (v: boolean) => void
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

function StatusPill({ ok, tone, children }: { ok: boolean; tone?: 'success' | 'warning' | 'neutral'; children: ReactNode }) {
  const resolvedTone = tone || (ok ? 'success' : 'warning')
  return (
    <span
      className="settings-status-pill text-[10px] font-[700] rounded-full px-2.5 py-1 shrink-0"
      data-tone={resolvedTone}
    >
      {children}
    </span>
  )
}

function sourceLabel(source: string): string {
  if (source === 'environment') return 'Process environment'
  if (source === 'user') return 'Desktop settings'
  if (source === 'dotenv') return '.env'
  return 'Built-in default'
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
  const storedValue = field.key && source === 'user' ? desktop?.values?.[field.key] : undefined
  const initial = storedValue !== undefined ? storedValue : field.value ?? ''
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
    setDraft(storedValue !== undefined ? storedValue : field.value ?? '')
  }, [storedValue, field.value])

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
          {field.description ? `${t(field.description)} · ` : ''}{t(sourceLabel(source))}{electronUnavailable ? ` · ${t('editable in Electron app')}` : environmentLocked ? ` · ${t('locked')}` : ''}{field.restart_required ? ` · ${t('restart required')}` : ''}
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
            {(field.options || []).map(option => {
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
            <option value="true">{t('On')}</option>
            <option value="false">{t('Off')}</option>
          </select>
        ) : (
          <input
            aria-label={t(field.label)}
            type={field.type === 'url' ? 'url' : field.type === 'number' ? 'number' : 'text'}
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

function SettingsGroup({ title, detail, children }: { title: string; detail?: string; children: ReactNode }) {
  return (
    <section>
      <GroupTitle detail={detail}>{title}</GroupTitle>
      <div className="settings-group-body">{children}</div>
    </section>
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

function ThemePicker({ value, onChange }: { value: UiTheme; onChange: (theme: UiTheme) => void }) {
  const { t } = useI18n()
  const choices: Array<{ id: UiTheme; title: string; description: string }> = [
    { id: 'classic', title: 'Classic light', description: 'Clean neutral desktop palette.' },
    { id: 'wallpaper-slice', title: 'Wallpaper slice', description: 'Dark translucent surfaces inspired by the Wallpaper Slice.' },
  ]
  return (
    <div className="settings-theme-picker" role="radiogroup" aria-label={t('Interface theme')}>
      {choices.map(choice => {
        const selected = value === choice.id
        return (
          <button
            key={choice.id}
            type="button"
            role="radio"
            aria-checked={selected}
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
              <span>{t(choice.description)}</span>
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
    return ['capabilities', 'general', 'graphics', 'models', 'voice', 'providers'].includes(String(saved))
      ? saved as SettingsSection
      : 'capabilities'
  })
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

  useEffect(() => {
    window.localStorage.setItem('amadeus.settings.section', section)
  }, [section])

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
    } catch (reason) {
      if (persisted) {
        setNotice('Saved for the next backend start; the current runtime did not change.')
      } else {
        setError(reason instanceof Error ? reason.message : `Could not update ${key}`)
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
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not save ${field.key}`)
      throw reason
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
    const currentMode = String(config.vision_mode ?? 'off')
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
  }, [send, config])

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

  const val = (key: string, fallback: string) => String(config[key] ?? runtimeSettingFromDesktopValues(key, desktop?.values) ?? fallback)
  const bool = (key: string) => Boolean(config[key] ?? runtimeSettingFromDesktopValues(key, desktop?.values))
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
  const runtimePackages: RuntimePackageStatus[] = [
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

  const modelConnections = asConfigurationGroups(config.model_connections)
  const backendModelRoles = asConfigurationGroups(config.model_roles)
  const modelRoleCatalog = buildModelRoleCatalog(desktop)
  const modelRoles: ConfigurationGroup[] = [
    ...modelRoleCatalog.map(base => {
      const backend = backendModelRoles.find(group => group.id === base.id)
      return backend ? {
        ...backend,
        ...base,
        fields: base.fields,
        active: backend.active ?? base.active,
        configured: backend.configured ?? base.configured,
        status: backend.status || base.status,
        status_ok: backend.status_ok ?? base.status_ok,
        status_detail: backend.status_detail,
      } : base
    }),
    ...backendModelRoles.filter(group => !modelRoleCatalog.some(base => base.id === group.id)),
  ]
  const savedMainModelProvider = desktop?.sources?.LLM_PROVIDER === 'user' ? desktop.values.LLM_PROVIDER : ''
  const mainModelProvider = val('llm_provider', savedMainModelProvider || 'deepseek').toLowerCase()
  const remoteModelConnectionIds = new Set(['deepseek', 'openai', 'gemini', 'bedrock'])
  const backendRemoteModelConnections = modelConnections.filter(group => remoteModelConnectionIds.has(group.id))
  const remoteModelConnections: ConfigurationGroup[] = backendRemoteModelConnections.length
    ? backendRemoteModelConnections
    : buildRemoteModelConnectionCatalog(mainModelProvider, desktop)
  const backendLocalModelConnections = modelConnections.filter(group => ['local', 'hybrid_local'].includes(group.id))
  const localModelConnections: ConfigurationGroup[] = backendLocalModelConnections.length
    ? backendLocalModelConnections
    : buildLocalModelConnectionCatalog(mainModelProvider, desktop)
  const backendOptionalModelConnections = modelConnections.filter(group => group.id === 'character_rag')
  const optionalModelConnections: ConfigurationGroup[] = backendOptionalModelConnections.length
    ? backendOptionalModelConnections
    : buildOptionalModelServiceCatalog(desktop)
  const modelProviderLabels: Record<string, string> = {
    deepseek: 'DeepSeek', openai: 'OpenAI-compatible', gemini: 'Gemini', bedrock: 'AWS Bedrock',
    local: 'Pure-local model', hybrid: 'Hybrid local + Bedrock', hybrid2: 'Hybrid local + DeepSeek', hybrid3: 'Hybrid local + OpenAI',
  }
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
  const selectedWorkProvider = String(
    desktop?.sources?.WORK_EXECUTION_PROVIDER === 'user'
      ? desktop.values.WORK_EXECUTION_PROVIDER
      : desktop?.sources?.COOPERATIVE_CHAT_PROVIDER === 'user'
        ? desktop.values.COOPERATIVE_CHAT_PROVIDER
      : val('cooperative_chat_provider', 'pi'),
  ).toLowerCase()
  const selectedCodingProvider = String(desktop?.sources?.WORK_CODING_PROVIDER === 'user'
    ? desktop.values.WORK_CODING_PROVIDER : val('work_coding_provider', 'codex')).toLowerCase()
  const workExecutionEnabled = desktop?.sources?.COOPERATIVE_CHAT_ENABLED === 'user'
    ? desktop.values.COOPERATIVE_CHAT_ENABLED === 'true'
    : config.cooperative_chat_enabled === undefined ? true : bool('cooperative_chat_enabled')
  const workProviderLabels: Record<string, string> = { codex: 'Codex agent', openclaw: 'OpenClaw agent', browser: 'Browser provider', pi: 'Pi daily agent' }
  const workProviderAssignment = `${t('Coding')}: ${workProviderLabels[selectedCodingProvider] || selectedCodingProvider} · ${t('Everyday execution')}: ${workProviderLabels[selectedWorkProvider] || selectedWorkProvider}`
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
  const graphicsConfiguration = buildGraphicsConfiguration(graphicsRuntime, desktop)
  const backendProviderConfiguration = asConfigurationGroups(config.work_provider_configuration)
  const providerCatalog = buildWorkProviderCatalog({ provider: selectedWorkProvider, enabled: workExecutionEnabled,
    codingProvider: selectedCodingProvider, roleCandidates: providerRoleCandidates }, desktop)
  const providerConfiguration: ConfigurationGroup[] = providerCatalog.connections.map(base => {
    const backend = backendProviderConfiguration.find(group => group.id === base.id)
    return backend ? {
      ...backend,
      ...base,
      fields: base.fields,
      active: base.active,
      configured: backend.configured ?? base.configured,
      status: backend.status || base.status,
      status_ok: backend.status_ok ?? base.status_ok,
      status_detail: backend.status_detail,
    } : base
  })
  const artifactConfiguration = asConfigurationGroups(config.artifact_configuration)
  const backendVoiceConfiguration = asConfigurationGroups(config.voice_configuration)
  const voiceCatalog = buildVoiceConfigurationCatalog({
        asrBackend: desktop?.sources?.ASR_BACKEND === 'user' ? desktop.values.ASR_BACKEND : val('asr_backend', 'qwen3_asr'),
        ttsBackend: desktop?.sources?.TTS_BACKEND === 'user' ? desktop.values.TTS_BACKEND : val('tts_backend', 'gpt_sovits'),
        wakeEnabled: desktop?.sources?.WAKE_ENABLED === 'user' ? desktop.values.WAKE_ENABLED === 'true' : bool('wake_enabled'),
        aecEnabled: desktop?.sources?.AEC_REALTIME_ENABLED === 'user' ? desktop.values.AEC_REALTIME_ENABLED === 'true' : config.aec_realtime_enabled === undefined ? true : bool('aec_realtime_enabled'),
      }, desktop)
  const voiceConfiguration: ConfigurationGroup[] = voiceCatalog.map(base => {
    const backend = backendVoiceConfiguration.find(group => group.id === base.id)
    return backend ? {
      ...backend,
      ...base,
      fields: base.fields,
      configured: backend.configured ?? base.configured,
      status: backend.status || base.status,
      status_ok: backend.status_ok ?? base.status_ok,
      status_detail: backend.status_detail,
    } : base
  })
  const primaryVoiceIds = new Set(['conversation_asr', 'speech_synthesis', 'wake_asr', 'acoustic_pipeline'])
  const primaryVoiceConfiguration = voiceConfiguration.filter(group => primaryVoiceIds.has(group.id))
  const advancedVoiceConfiguration = voiceConfiguration.filter(group => !primaryVoiceIds.has(group.id))
  const visionWindowHandle = String(config.vision_window_handle
    ?? (desktop?.sources?.AMADEUS_VISION_WINDOW_HANDLE === 'user' ? desktop.values.AMADEUS_VISION_WINDOW_HANDLE : ''))
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
  const avatarConfiguration = asConfigurationGroups(config.avatar_configuration)
  const capabilityProfiles = useMemo(
    () => buildCapabilityProfiles(config, providerAvailability),
    [config, providerAvailability],
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
  }, [])

  return (
    <div className="settings-scroll-area flex-1 overflow-y-auto">
      <div style={{ width: 'min(100%, 1010px)', padding: '20px 24px 32px' }}>
        <div className="flex items-center justify-between gap-4" style={{ marginBottom: 16 }}>
          <div>
            <h2 className="settings-page-title">{t('Settings')}</h2>
            <div className="settings-page-context">
              {t(section === 'capabilities' ? 'Shared capabilities, implementations, and scene use.' : 'Runtime controls and desktop connection profiles.')}
            </div>
          </div>
          {restartPending ? (
            <button onClick={() => void restartBackend()} disabled={restarting} className="text-[11px] font-[600] rounded-md disabled:opacity-50" style={{ height: 32, padding: '0 12px', whiteSpace: 'nowrap', flexShrink: 0, color: 'white', background: 'var(--accent)', border: 0 }}>
              {t(restarting ? 'Restarting…' : 'Restart backend to apply')}
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

        <div className="settings-layout flex gap-6 items-start" style={{ width: '100%' }}>
          <nav className="settings-section-nav shrink-0 flex flex-col gap-0.5 sticky" style={{ width: 138, top: 16 }} aria-label={t('Settings sections')}>
            {([
              ['capabilities', 'Capabilities', 'Tiles'],
              ['general', 'General', 'Setting'],
              ['graphics', 'Graphics & performance', 'Video'],
              ['models', 'Models', 'Robot'],
              ['voice', 'Voice', 'Microphone'],
              ['providers', 'Providers', 'Work'],
            ] as Array<[SettingsSection, string, FluentIconName]>).map(([id, label, icon]) => (
              <button key={id} onClick={() => setSection(id)} aria-current={section === id ? 'page' : undefined} className="flex items-center gap-2 text-[11.5px] text-left rounded-md px-2.5" style={{ height: 32, color: section === id ? 'var(--text)' : 'var(--muted)', background: section === id ? 'var(--selected-fill)' : 'transparent', border: 0, fontWeight: section === id ? 650 : 500 }}>
                <FluentIcon name={icon} size={14} />{t(label)}
              </button>
            ))}
          </nav>

          <main className="settings-main flex-1 min-w-0" style={{ maxWidth: 760 }}>
            {section === 'capabilities' ? (
              <CapabilitiesPanel capabilities={capabilityProfiles} runtimePackages={runtimePackages} onOpenSection={openCapabilityTarget} />
            ) : null}

            {section === 'graphics' ? (
              <div className="flex flex-col gap-5">
                <BoundaryNote title="Graphics & performance">
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
                  <SettingsGroup title="Startup" detail="Quit and reopen Amadeus to apply. Wallpaper is always available in the sidebar.">
                    <ComboCard icon="Setting" title="Startup mode"
                      content={desktop.locked?.AMADEUS_WINDOWS_STARTUP_MODE
                        ? 'Startup mode is controlled by your launch environment.'
                        : 'Choose whether to open the control panel first or enter wallpaper directly.'}
                      value={desktop.values.AMADEUS_WINDOWS_STARTUP_MODE || DEFAULT_WINDOWS_STARTUP_MODE}
                      disabled={saving === 'AMADEUS_WINDOWS_STARTUP_MODE' || desktop.locked?.AMADEUS_WINDOWS_STARTUP_MODE}
                      options={[{ value: 'window', label: 'Open control panel first' }, { value: 'wallpaper', label: 'Enter wallpaper directly' }]}
                      onChange={value => {
                        void handleStartupSave({ key: 'AMADEUS_WINDOWS_STARTUP_MODE', label: 'Startup mode',
                          type: 'select', editable: true, restart_required: false }, value, false)
                          .then(() => setNotice('Startup mode saved. Quit and reopen Amadeus to apply.'))
                          .catch(() => { /* handleStartupSave displays the save error. */ })
                      }}
                    />
                  </SettingsGroup>
                ) : null}
                <SettingsGroup title="Appearance" detail="Choose the visual style used across the Electron frontend.">
                  <ThemePicker
                    value={theme}
                    onChange={value => void setTheme(value).catch(reason => setError(reason instanceof Error ? reason.message : 'Could not save interface theme'))}
                  />
                </SettingsGroup>
                <SettingsGroup title="Chat appearance" detail="Local presentation only; avatar images are never sent to the model.">
                  <ChatAvatarSettings />
                </SettingsGroup>
                <SettingsGroup title="Avatar compatibility" detail="Optional output paths are disabled unless explicitly enabled.">
                  {avatarConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} />)}
                </SettingsGroup>
                <div id="settings-language"><SettingsGroup title="Language & captions" detail="Desktop settings are saved across restarts and applied to the current runtime immediately when possible.">
                  <ComboCard
                    icon="Language"
                    title="Console language"
                    content="Controls Electron navigation, Settings, and capability status pages."
                    value={locale}
                    onChange={value => void setLocale(value as UiLocale).catch(reason => setError(reason instanceof Error ? reason.message : 'Could not save console language'))}
                    options={[
                      { value: 'en-US', label: 'English' },
                      { value: 'zh-CN', label: 'Simplified Chinese' },
                    ]}
                  />
                  <ComboCard icon="Language" title="Slice Language" content="Language for process cards and Provider summaries" value={val('presentation_locale', 'en-US')} onChange={value => handleChange('presentation_locale', value)} options={['en-US', 'zh-CN', 'ja-JP']} />
                  <SwitchCard icon="Language" title="Chat Translation Subtitles" content="Show Simplified Chinese below completed Japanese assistant messages. Display-only; never added to conversation history." checked={bool('chat_translation_subtitles_enabled')} onChange={value => handleChange('chat_translation_subtitles_enabled', value)} />
                  <ComboCard icon="Language" title="Wallpaper Caption Mode" content="Choose translated, source, bilingual, or no captions" value={val('wallpaper_caption_mode', 'translated')} onChange={value => handleChange('wallpaper_caption_mode', value)} options={['translated', 'source', 'bilingual', 'off']} />
                </SettingsGroup></div>
                <div id="settings-vision"><SettingsGroup title="Multimodal & Vision" detail="Visual understanding always uses the Main conversation model; these controls only govern capture behavior.">
                  <SwitchCard icon="Camera" title="Vision Context" content="Attach a scoped screenshot to visual chat turns" checked={bool('vision_enabled')} onChange={handleVisionEnabled} />
                  <ComboCard icon="Video" title="Vision Mode" content="On-demand captures when asked; watching attaches one fresh frame to each chat turn" value={val('vision_mode', 'off')} onChange={value => handleChange('vision_mode', value)} options={['off', 'on_demand', 'watching', 'self_aware']} disabled={!bool('vision_enabled')} />
                  <ComboCard icon="Video" title="Vision Scope" content="Choose what Amadeus may capture for a visual turn" value={val('vision_scope', 'full_screen')} onChange={value => handleChange('vision_scope', value)} options={['full_screen', 'current_window', 'selected_window']} disabled={!bool('vision_enabled')} />
                  {bool('vision_enabled') && val('vision_scope', 'full_screen') === 'selected_window' ? (
                    visionWindows.length || visionWindowHandle ? (
                      <ComboCard
                        icon="Tiles"
                        title="Vision target window"
                        content="Capture stops if this window closes or can no longer be verified."
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
                  <ComboCard icon="Photo" title="Vision Image Size" content="Maximum long edge sent to the model" value={val('vision_max_long_side', '960')} onChange={value => handleChange('vision_max_long_side', Number(value))} options={['640', '960', '1280', '1600']} disabled={!bool('vision_enabled')} />
                  <ComboCard icon="Photo" title="Vision JPEG Quality" content="Higher quality increases request payload size" value={val('vision_jpeg_quality', '68')} onChange={value => handleChange('vision_jpeg_quality', Number(value))} options={['50', '68', '80', '90']} disabled={!bool('vision_enabled')} />
                </SettingsGroup></div>
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
                      <ComboCard icon="Robot" title="Main conversation" content="Primary model assignment shared by Chat and Wallpaper-originated turns" value={mainModelProvider} onChange={value => void handleMainProviderChange(value)} options={[
                        { value: 'deepseek', label: 'DeepSeek' },
                        { value: 'openai', label: 'OpenAI-compatible' },
                        { value: 'gemini', label: 'Gemini' },
                        { value: 'bedrock', label: 'AWS Bedrock' },
                        { value: 'local', label: 'Pure-local model' },
                        { value: 'hybrid', label: 'Hybrid · Local + Bedrock' },
                        { value: 'hybrid2', label: 'Hybrid · Local + DeepSeek' },
                        { value: 'hybrid3', label: 'Hybrid · Local + OpenAI-compatible' },
                      ]} />
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
                    <SettingsGroup title="Optional model services" detail="Supporting services shared by one or more model roles.">
                      {optionalModelConnections.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible defaultOpen={Boolean(group.active)} optionalWhenInactive />)}
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
                <SettingsGroup title="Voice backends" detail="Startup configuration. Secrets are encrypted by the operating system and never returned to this page.">
                  {primaryVoiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible optionalWhenInactive />)}
                </SettingsGroup>
                <SettingsGroup title="Speech language" detail="User-facing language for generated speech.">
                  <ComboCard icon="Language" title="TTS output language" content="Language used for sentence splitting and the matching voice reference." value={val('tts_output_language', 'ja')} onChange={value => handleChange('tts_output_language', value)} options={[{ value: 'ja', label: 'Japanese' }, { value: 'en', label: 'English' }]} />
                </SettingsGroup>
                <details className="model-advanced-roles">
                  <summary>
                    <span>{t('Advanced voice settings')}</span>
                    <small>{t('Engine paths, remote endpoints, and performance')}</small>
                  </summary>
                  <div className="flex flex-col gap-5">
                    <SettingsGroup title="Local speech performance" detail="Performance tuning for the embedded GPT-SoVITS engine.">
                      <ComboCard icon="Tiles" title="Local TTS inference mode" content="Choose standard single synthesis, CUDA Graph, or explicit parallel generation while speech is idle." value={val('tts_mode', 'parallel')} onChange={value => handleChange('tts_mode', value)} options={[{ value: 'parallel', label: 'Standard ×1' }, { value: 'cuda_graph', label: 'CUDA Graph ×1' }, { value: 'parallel2', label: 'Parallel ×2' }]} disabled={val('tts_backend', 'gpt_sovits') !== 'gpt_sovits'} />
                    </SettingsGroup>
                    <SettingsGroup title="Voice implementation details" detail="Paths, reference audio, and endpoint settings for the implementations selected above.">
                      {advancedVoiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} collapsible optionalWhenInactive />)}
                    </SettingsGroup>
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
