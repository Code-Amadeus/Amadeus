import { useState, useEffect, useCallback, useMemo, type ReactNode } from 'react'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import McpConnections, { type McpConnectionSummary } from './McpConnections'
import ChatAvatarSettings from './ChatAvatarSettings'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
}

type SettingsSection = 'general' | 'models' | 'voice' | 'providers'

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
  values: Record<string, string>
  sources: Record<string, 'environment' | 'user' | 'dotenv' | 'default'>
  locked: Record<string, boolean>
  secrets: Record<string, { configured: boolean; source: string; locked: boolean }>
  encryptionAvailable: boolean
  mcpConnections: McpConnectionSummary[]
  mcpConnectionsLocked: boolean
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
  return (
    <div>
      <h3 className="text-[14px] font-[700]" style={{ color: 'var(--text)', lineHeight: '20px', letterSpacing: '-0.012em' }}>{children}</h3>
      {detail ? <div className="text-[11.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '17px' }}>{detail}</div> : null}
    </div>
  )
}

function CardShell({ children, vertical = false }: { children: ReactNode; vertical?: boolean }) {
  return (
    <div
      className={`setting-card flex ${vertical ? 'flex-col items-stretch' : 'items-center'}`}
      style={{
        minHeight: vertical ? undefined : 62,
        backgroundColor: 'var(--surface)',
        border: '1px solid rgba(17,24,39,0.085)',
        borderRadius: 11,
        padding: vertical ? '12px 14px' : '10px 14px',
        boxShadow: '0 1px 2px rgba(17,24,39,0.025)',
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
  return (
    <CardShell>
      <CardIcon name={icon} />
      <div className="flex-1 min-w-0" style={{ paddingRight: 16 }}>
        <div className="text-[13px] font-[600]" style={{ color: 'var(--text)', lineHeight: '19px' }}>{title}</div>
        {content ? <div className="text-[11.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '17px' }}>{content}</div> : null}
      </div>
      <select
        value={value}
        onChange={event => onChange(event.target.value)}
        disabled={disabled}
        className="text-[12px] bg-white border border-[var(--border)] rounded-lg px-3 text-[var(--text)] outline-none hover:border-[var(--border-strong)] focus:border-[var(--accent)] disabled:opacity-40 shrink-0"
        style={{ minWidth: 145, height: 35 }}
      >
        {options.map(option => {
          const optionValue = typeof option === 'string' ? option : option.value
          const label = typeof option === 'string' ? option : option.label
          return <option key={optionValue} value={optionValue}>{label}</option>
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
  return (
    <CardShell>
      <CardIcon name={icon} />
      <div className="flex-1 min-w-0" style={{ paddingRight: 16 }}>
        <div className="text-[13px] font-[600]" style={{ color: 'var(--text)', lineHeight: '19px' }}>{title}</div>
        {content ? <div className="text-[11.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '17px' }}>{content}</div> : null}
      </div>
      <button
        onClick={() => onChange(!checked)}
        className="relative shrink-0 transition-colors cursor-pointer"
        style={{ width: 38, height: 22, borderRadius: 11, backgroundColor: checked ? '#0078D4' : 'var(--border-strong)', border: 'none' }}
        aria-pressed={checked}
      >
        <span className="absolute top-[3px] left-0 w-4 h-4 rounded-full bg-white shadow transition-transform" style={{ transform: checked ? 'translateX(19px)' : 'translateX(3px)' }} />
      </button>
      <span className="text-[11px] shrink-0 ml-2" style={{ color: 'var(--muted)', width: 24 }}>{checked ? 'On' : 'Off'}</span>
    </CardShell>
  )
}

function InfoCard({ icon, title, content, value }: {
  icon: FluentIconName; title: string; content: string; value: string
}) {
  return (
    <CardShell>
      <CardIcon name={icon} />
      <div className="flex-1 min-w-0" style={{ paddingRight: 16 }}>
        <div className="text-[13px] font-[600]" style={{ color: 'var(--text)', lineHeight: '19px' }}>{title}</div>
        {content ? <div className="text-[11.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '17px' }}>{content}</div> : null}
      </div>
      <span className="shrink-0 text-[12px] font-[550] text-right" style={{ color: 'var(--text)', maxWidth: 300 }}>{value}</span>
    </CardShell>
  )
}

function StatusPill({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span
      className="text-[10px] font-[700] rounded-full px-2.5 py-1"
      style={{ color: ok ? '#107C10' : '#8A5414', backgroundColor: ok ? '#E8F5E9' : '#FFF4CE' }}
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
  const source = field.key ? desktop?.sources?.[field.key] || 'default' : 'default'
  const storedValue = field.key && source === 'user' ? desktop?.values?.[field.key] : undefined
  const initial = storedValue !== undefined ? storedValue : field.value ?? ''
  const [draft, setDraft] = useState<string | boolean>(initial)
  const [busy, setBusy] = useState(false)
  const [secretDraft, setSecretDraft] = useState('')
  const locked = field.key ? Boolean(desktop?.locked?.[field.key]) : true
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
    <div className="settings-field-row flex items-start gap-5" style={{ padding: '10px 0', borderTop: '1px solid rgba(17,24,39,0.075)' }}>
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-[600]" style={{ color: 'var(--text)', lineHeight: '18px', letterSpacing: '-0.006em' }}>{field.label}</div>
        <div className="text-[11px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '16px' }}>
          {field.description ? `${field.description} · ` : ''}{sourceLabel(source)}{locked ? ' · locked' : ''}{field.restart_required ? ' · restart required' : ''}
        </div>
      </div>
      <div className="settings-field-control flex items-center gap-1.5 shrink-0" style={{ width: 320, maxWidth: '43%' }}>
        {!field.editable || !field.key ? (
          <span className="text-[13px] truncate ml-auto" style={{ color: 'var(--text)' }}>{String(field.value ?? '')}</span>
        ) : field.type === 'secret' ? (
          <div className="relative min-w-0 flex-1">
            <input
              type="password"
              value={secretDraft}
              onChange={event => setSecretDraft(event.target.value)}
              disabled={locked || busy}
              placeholder={secretConfigured ? 'Configured — enter to replace' : 'Not configured'}
              autoComplete="new-password"
              onKeyDown={event => {
                if (event.key === 'Enter' && secretDraft && !locked && !busy) {
                  void save(secretDraft, true)
                }
              }}
              className="w-full min-w-0 text-[12px] bg-white border border-[var(--border)] rounded-lg pl-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
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
                    label={`Save ${field.label}`}
                    glyph="✓"
                    busy={busy}
                    disabled={locked || busy}
                    onClick={() => void save(secretDraft, true)}
                  />
                ) : null}
                {secretConfigured ? (
                  <InlineFieldAction
                    label={`Clear ${field.label}`}
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
            value={String(draft)}
            onChange={event => {
              const value = event.target.value
              setDraft(value)
              if (value !== String(initial)) void save(value, false)
            }}
            disabled={locked || busy}
            className="min-w-0 flex-1 text-[12px] bg-white border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
            style={{ height: 34 }}
          >
            {(field.options || []).map(option => {
              const optionValue = typeof option === 'string' ? option : option.value
              const optionLabel = typeof option === 'string' ? option || 'default' : option.label
              return <option key={optionValue} value={optionValue}>{optionLabel}</option>
            })}
          </select>
        ) : field.type === 'boolean' ? (
          <select
            value={String(draft)}
            onChange={event => {
              const value = event.target.value === 'true'
              setDraft(value)
              if (String(value) !== String(initial)) void save(value, false)
            }}
            disabled={locked || busy}
            className="min-w-0 flex-1 text-[12px] bg-white border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
            style={{ height: 34 }}
          >
            <option value="true">On</option>
            <option value="false">Off</option>
          </select>
        ) : (
          <input
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
            className="min-w-0 flex-1 text-[12px] bg-white border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
            style={{ height: 34 }}
          />
        )}
        {busy && field.type !== 'secret' ? <span className="text-[10px] shrink-0" style={{ color: 'var(--muted)' }}>Saving…</span> : null}
      </div>
    </div>
  )
}

function ConfigurationCard({ group, desktop, availability, onSave }: {
  group: ConfigurationGroup
  desktop: DesktopSettingsSnapshot | null
  availability?: ProviderAvailability
  onSave: (field: StartupField, value: string | boolean | null, secret: boolean) => Promise<void>
}) {
  const statusOk = group.status_ok ?? (availability ? availability.ready && availability.registered : Boolean(group.configured))
  const statusText = availability
    ? statusOk ? 'Registered' : availability.reason || 'Unavailable'
    : group.status
      ? group.status.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase())
      : group.configured ? 'Configured' : 'Needs setup'
  return (
    <CardShell vertical>
      <div className="flex items-start gap-2.5 pb-2">
        <div className="flex items-center justify-center mt-0.5" style={{ width: 24, color: 'var(--muted)' }}>
          <FluentIcon name={group.id === 'local' ? 'CommandPrompt' : 'Robot'} size={17} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <div className="text-[14px] font-[700]" style={{ color: 'var(--text)', lineHeight: '19px' }}>{group.label}</div>
            {group.active ? <span className="text-[9px] font-[700]" style={{ color: 'var(--accent)' }}>ACTIVE</span> : null}
          </div>
          {group.description ? <div className="text-[11px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '16px' }}>{group.description}</div> : null}
          {group.status_detail ? <div className="text-[10.5px] mt-0.5" style={{ color: 'var(--faint)', lineHeight: '15px' }}>{group.status_detail}</div> : null}
        </div>
        <StatusPill ok={statusOk}>{statusText}</StatusPill>
      </div>
      {group.fields.map(field => <StartupFieldRow key={`${group.id}-${field.key || field.label}`} field={field} desktop={desktop} onSave={onSave} />)}
      {group.fields.length === 0 ? <div className="text-[11px] pt-2" style={{ color: 'var(--muted)', borderTop: '1px solid var(--border)' }}>No user-managed connection settings.</div> : null}
    </CardShell>
  )
}

function CapabilityCard({ contribution, packageInfo, consumers }: {
  contribution: CapabilityContribution
  packageInfo: CapabilityPackage
  consumers: string[]
}) {
  const bindings = (contribution.bindings || []).filter(binding => binding.enabled)
  return (
    <CardShell vertical>
      <div className="flex items-start gap-2.5">
        <div className="flex items-center justify-center mt-0.5" style={{ width: 22, color: 'var(--muted)' }}>
          <FluentIcon name={contribution.kind === 'skill' ? 'Work' : 'CommandPrompt'} size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-[650]" style={{ color: 'var(--text)' }}>{contribution.id}</span>
            <span className="text-[9px] uppercase tracking-wide" style={{ color: 'var(--muted)' }}>{contribution.kind === 'mcp_server' ? 'MCP' : 'Skill'}</span>
          </div>
          <div className="text-[10.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '15px' }}>{contribution.summary}</div>
        </div>
        <StatusPill ok={contribution.available}>{contribution.available ? 'Available' : contribution.health}</StatusPill>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 mt-2.5 pt-2.5 text-[9.5px]" style={{ borderTop: '1px solid rgba(17,24,39,0.075)', color: 'var(--muted)' }}>
        <div><span className="font-[600]">Consumers:</span> {consumers.length ? consumers.join(', ') : 'No active Provider binding'}</div>
        <div><span className="font-[600]">Scope:</span> Work Providers only</div>
        <div><span className="font-[600]">Projection:</span> {bindings.map(item => item.projection).join(', ') || 'None'}</div>
        <div><span className="font-[600]">Source:</span> {packageInfo.source} · {packageInfo.trust}</div>
      </div>
    </CardShell>
  )
}

function SettingsGroup({ title, detail, children }: { title: string; detail?: string; children: ReactNode }) {
  return (
    <section>
      <GroupTitle detail={detail}>{title}</GroupTitle>
      <div className="flex flex-col" style={{ gap: 8, marginTop: 14 }}>{children}</div>
    </section>
  )
}

function BoundaryNote({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div
      className="rounded-lg"
      style={{
        padding: '10px 12px',
        border: '1px solid rgba(0,120,212,0.18)',
        background: 'rgba(0,120,212,0.035)',
      }}
    >
      <div className="text-[11.5px] font-[650]" style={{ color: 'var(--text)', lineHeight: '16px' }}>{title}</div>
      <div className="text-[10.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '15px' }}>{children}</div>
    </div>
  )
}

export default function SettingsPage({ send, subscribe }: Props) {
  const [section, setSection] = useState<SettingsSection>('general')
  const [config, setConfig] = useState<Record<string, unknown>>({})
  const [providerAvailability, setProviderAvailability] = useState<ProviderAvailability[]>([])
  const [providerManifests, setProviderManifests] = useState<ProviderManifest[]>([])
  const [capabilityPackages, setCapabilityPackages] = useState<CapabilityPackage[]>([])
  const [desktop, setDesktop] = useState<DesktopSettingsSnapshot | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  const [restartPending, setRestartPending] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState('')

  const refreshDesktop = useCallback(async () => {
    const snapshot = await window.amadeus?.getDesktopSettings()
    if (snapshot) setDesktop(snapshot as unknown as DesktopSettingsSnapshot)
  }, [])

  useEffect(() => {
    const unsubscribe = subscribe('system.config', payload => {
      setConfig((payload.values as Record<string, unknown>) ?? payload)
    })
    Promise.all([
      send('system.get_config', {}).then(response => setConfig((response.values as Record<string, unknown>) ?? response)),
      send('provider.list', {}).then(response => {
        setProviderAvailability(Array.isArray(response.provider_availability) ? response.provider_availability as unknown as ProviderAvailability[] : [])
        setProviderManifests(Array.isArray(response.provider_manifests) ? response.provider_manifests as unknown as ProviderManifest[] : [])
      }),
      send('capability.list', { include_disabled: true }).then(response => {
        setCapabilityPackages(Array.isArray(response.packages) ? response.packages as unknown as CapabilityPackage[] : [])
      }),
      refreshDesktop(),
    ]).catch(reason => setError(reason instanceof Error ? reason.message : 'Could not load settings'))
    return unsubscribe
  }, [subscribe, send, refreshDesktop])

  const handleChange = useCallback(async (key: string, value: unknown) => {
    setSaving(key)
    setError('')
    try {
      const response = await send('system.set_config', { values: { [key]: value } })
      setConfig((response.values as Record<string, unknown>) ?? response)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not update ${key}`)
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
    try {
      const result = await window.amadeus.updateDesktopSettings(
        secret ? { secrets: { [field.key]: value as string | null } } : { values: { [field.key]: value } },
      )
      if (!result.ok) throw new Error(result.error || `Could not save ${field.key}`)
      if (result.settings) setDesktop(result.settings as unknown as DesktopSettingsSnapshot)
      setRestartPending(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not save ${field.key}`)
      throw reason
    } finally {
      setSaving(null)
    }
  }, [])

  const restartBackend = useCallback(async () => {
    if (!window.amadeus) return
    setRestarting(true)
    setError('')
    try {
      const ok = await window.amadeus.restartBackend()
      if (!ok) throw new Error('Backend restart failed')
      setRestartPending(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Backend restart failed')
    } finally {
      setRestarting(false)
    }
  }, [])

  const handleVisionEnabled = useCallback(async (value: boolean) => {
    const currentMode = String(config.vision_mode ?? 'off')
    const nextMode = value && currentMode === 'off' ? 'on_demand' : currentMode
    setSaving('vision_enabled')
    setError('')
    try {
      const response = await send('system.set_config', { values: { vision_enabled: value, vision_mode: value ? nextMode : 'off' } })
      setConfig((response.values as Record<string, unknown>) ?? response)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not update vision')
    } finally {
      setSaving(null)
    }
  }, [send, config])

  const val = (key: string, fallback: string) => String(config[key] ?? fallback)
  const bool = (key: string) => Boolean(config[key])
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

  const modelConnections = asConfigurationGroups(config.model_connections)
  const modelRoles = asConfigurationGroups(config.model_roles)
  const providerConfiguration = asConfigurationGroups(config.work_provider_configuration)
  const voiceConfiguration = asConfigurationGroups(config.voice_configuration)
  const avatarConfiguration = asConfigurationGroups(config.avatar_configuration)
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

  return (
    <div className="settings-scroll-area flex-1 overflow-y-auto">
      <div style={{ width: 'min(100%, 1010px)', padding: '20px 24px 32px' }}>
        <div className="flex items-center justify-between gap-4" style={{ marginBottom: 16 }}>
          <div>
            <h2 className="font-[650]" style={{ color: 'var(--text)', fontSize: 19, lineHeight: '25px', letterSpacing: '-0.02em' }}>Settings</h2>
            <div className="text-[10.5px] mt-0.5" style={{ color: 'var(--muted)' }}>Runtime controls and desktop connection profiles.</div>
          </div>
          {restartPending ? (
            <button onClick={() => void restartBackend()} disabled={restarting} className="text-[11px] font-[600] rounded-md px-3 disabled:opacity-50" style={{ height: 32, color: 'white', background: 'var(--accent)', border: 0 }}>
              {restarting ? 'Restarting…' : 'Restart backend to apply'}
            </button>
          ) : null}
        </div>

        <div className="settings-layout flex gap-6 items-start" style={{ width: '100%' }}>
          <nav className="settings-section-nav shrink-0 flex flex-col gap-0.5 sticky" style={{ width: 138, top: 16 }} aria-label="Settings sections">
            {([
              ['general', 'General', 'Setting'],
              ['models', 'Models', 'Robot'],
              ['voice', 'Voice', 'Microphone'],
              ['providers', 'Providers', 'Work'],
            ] as Array<[SettingsSection, string, FluentIconName]>).map(([id, label, icon]) => (
              <button key={id} onClick={() => setSection(id)} className="flex items-center gap-2 text-[11.5px] text-left rounded-md px-2.5" style={{ height: 32, color: section === id ? 'var(--text)' : 'var(--muted)', background: section === id ? 'rgba(17,24,39,0.055)' : 'transparent', border: 0, fontWeight: section === id ? 650 : 500 }}>
                <FluentIcon name={icon} size={14} />{label}
              </button>
            ))}
          </nav>

          <main className="settings-main flex-1 min-w-0" style={{ maxWidth: 760 }}>
            {section === 'general' ? (
              <div className="flex flex-col gap-5">
                <SettingsGroup title="Optional Runtime Assets">
                  <InfoCard icon="Photo" title="Visual Runtime Pack" content={visualPackContent} value={visualPackValue} />
                  <InfoCard icon="People" title="Kurisu Character Pack" content={characterPackContent} value={characterPackValue} />
                </SettingsGroup>
                <SettingsGroup title="Chat appearance" detail="Local presentation only; avatar images are never sent to the model.">
                  <ChatAvatarSettings />
                </SettingsGroup>
                <SettingsGroup title="Avatar compatibility" detail="Optional output paths are disabled unless explicitly enabled.">
                  {avatarConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} />)}
                </SettingsGroup>
                <SettingsGroup title="Multimodal & Vision">
                  <SwitchCard icon="Camera" title="Vision Context" content="Attach a scoped screenshot to visual chat turns" checked={bool('vision_enabled')} onChange={handleVisionEnabled} />
                  <ComboCard icon="Video" title="Vision Mode" content="On-demand captures when asked; watching attaches one fresh frame to each chat turn" value={val('vision_mode', 'off')} onChange={value => handleChange('vision_mode', value)} options={['off', 'on_demand', 'watching', 'self_aware']} disabled={!bool('vision_enabled')} />
                  <ComboCard icon="Video" title="Vision Scope" content="Choose what Amadeus may capture for a visual turn" value={val('vision_scope', 'full_screen')} onChange={value => handleChange('vision_scope', value)} options={['full_screen', 'current_window', 'selected_window', 'wallpaper_surface', 'region']} disabled={!bool('vision_enabled')} />
                  <ComboCard icon="Robot" title="Vision Provider" content="Direct image input is supported first by OpenAI and Gemini" value={val('vision_provider', 'auto')} onChange={value => handleChange('vision_provider', value)} options={['auto', 'openai', 'gemini', 'deepseek', 'custom']} disabled={!bool('vision_enabled')} />
                  <ComboCard icon="Photo" title="Vision Image Size" content="Maximum long edge sent to the model" value={val('vision_max_long_side', '960')} onChange={value => handleChange('vision_max_long_side', Number(value))} options={['640', '960', '1280', '1600']} disabled={!bool('vision_enabled')} />
                  <ComboCard icon="Photo" title="Vision JPEG Quality" content="Higher quality increases request payload size" value={val('vision_jpeg_quality', '68')} onChange={value => handleChange('vision_jpeg_quality', Number(value))} options={['50', '68', '80', '90']} disabled={!bool('vision_enabled')} />
                </SettingsGroup>
                <SettingsGroup title="Chat & Voice">
                  <ComboCard icon="Language" title="Slice Language" content="Language for process cards and Provider summaries" value={val('presentation_locale', 'en-US')} onChange={value => handleChange('presentation_locale', value)} options={['en-US', 'zh-CN', 'ja-JP']} />
                  <ComboCard icon="Language" title="Wallpaper Caption Mode" content="Choose translated, source, bilingual, or no captions" value={val('wallpaper_caption_mode', 'translated')} onChange={value => handleChange('wallpaper_caption_mode', value)} options={['translated', 'source', 'bilingual', 'off']} />
                </SettingsGroup>
              </div>
            ) : null}

            {section === 'models' ? (
              <div className="flex flex-col gap-5">
                <SettingsGroup title="Main chat model" detail="These controls affect the current backend process. Connection edits below are desktop defaults and require a restart.">
                  <ComboCard icon="Robot" title="Current model profile" content="The model used by the main conversational role" value={val('llm_provider', 'local')} onChange={value => handleChange('llm_provider', value)} options={['local', 'deepseek', 'openai', 'gemini', 'bedrock', 'hybrid', 'hybrid2', 'hybrid3']} />
                  <ComboCard icon="CommandPrompt" title="Pure-local Backend Type" content="llama.cpp server is the default; Hybrid uses its dedicated local-head endpoint below" value={val('local_llm_type', 'llama_server')} onChange={value => handleChange('local_llm_type', value)} options={['llama_server', 'lmstudio', 'ollama', 'cli']} disabled={val('llm_provider', 'local') !== 'local'} />
                </SettingsGroup>
                <SettingsGroup title="Model connections" detail="Secrets are encrypted by the operating system and are never returned to the renderer.">
                  {modelConnections.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} />)}
                </SettingsGroup>
                <details>
                  <summary className="text-[12px] font-[600] cursor-pointer" style={{ color: 'var(--text)' }}>Advanced model roles</summary>
                  <div className="flex flex-col gap-8 mt-3">
                    {modelRoles.map(group => (
                      <div key={group.id}>
                        <GroupTitle detail={group.description}>{group.label}</GroupTitle>
                        <div className="mt-2"><ConfigurationCard group={group} desktop={desktop} onSave={handleStartupSave} /></div>
                      </div>
                    ))}
                  </div>
                </details>
              </div>
            ) : null}

            {section === 'voice' ? (
              <div className="flex flex-col gap-5">
                <BoundaryNote title="Voice data boundary">
                  Wake and Conversation recognition are independent roles. Selecting a remote backend sends confirmed conversation audio or synthesis text to the configured endpoint; Amadeus never silently falls back from local to remote.
                </BoundaryNote>
                <SettingsGroup title="Runtime presentation" detail="These controls apply immediately and do not select the synthesis or recognition provider.">
                  <ComboCard icon="Tiles" title="Local TTS inference mode" content="Used by the Amadeus GPT-SoVITS v3 rewrite; switch while TTS is idle" value={val('tts_mode', 'parallel')} onChange={value => handleChange('tts_mode', value)} options={[{ value: 'cuda_graph', label: 'CUDA Graph ×1' }, { value: 'parallel', label: 'Parallel ×2' }]} disabled={val('tts_backend', 'gpt_sovits') !== 'gpt_sovits'} />
                  <ComboCard icon="Language" title="TTS output language" content="Updates sentence splitting, cache keys, and embedded reference selection" value={val('tts_output_language', 'ja')} onChange={value => handleChange('tts_output_language', value)} options={[{ value: 'ja', label: 'Japanese' }, { value: 'en', label: 'English' }]} />
                </SettingsGroup>
                <SettingsGroup title="Voice backends" detail="Startup configuration. Secrets are encrypted by the operating system and never returned to this page.">
                  {voiceConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} onSave={handleStartupSave} />)}
                </SettingsGroup>
              </div>
            ) : null}

            {section === 'providers' ? (
              <div className="flex flex-col gap-5">
                <BoundaryNote title="Execution boundary">
                  Main Chat may delegate work to a Provider. Skills and MCP connections are shared only among compatible Work Providers; their prompts and tool schemas are never attached directly to Main Chat.
                </BoundaryNote>
                <SettingsGroup title="Work Provider connections" detail="Registered means the adapter passed its startup boundary. Remote availability is verified when that Provider connects.">
                  {providerConfiguration.map(group => <ConfigurationCard key={group.id} group={group} desktop={desktop} availability={providerAvailability.find(item => item.provider_id === group.id)} onSave={handleStartupSave} />)}
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
                  {!sharedCapabilities.length ? <div className="text-[10.5px]" style={{ color: 'var(--muted)' }}>No shared Provider capability is active in this backend process.</div> : null}
                </SettingsGroup>
              </div>
            ) : null}

            {saving ? <div className="text-[11px] animate-pulse mt-4" style={{ color: 'var(--accent)' }}>Saving {saving}…</div> : null}
            {error ? <div className="text-[11px] mt-4 rounded-md p-3" style={{ color: '#b42318', background: '#FEF3F2' }}>{error}</div> : null}
          </main>
        </div>
      </div>
    </div>
  )
}
