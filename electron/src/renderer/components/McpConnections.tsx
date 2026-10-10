import { useEffect, useMemo, useRef, useState } from 'react'
import FluentIcon from './FluentIcon'
import { useI18n } from '../i18n'
import { SettingsButton, SettingsField, StatusPill } from './SettingsPrimitives'
import { canLeaveSettingsEditors, DISCARD_CONNECTION_CHANGES, type SettingsEditorState } from './settingsDraft'

export interface McpConnectionSummary {
  id: string
  name: string
  enabled: boolean
  transport: 'stdio' | 'http'
  providerIds: string[]
  command: string
  arguments: string[]
  cwd: string
  url: string
  bearerTokenEnvVar: string
  environmentKeys: string[]
  mainChatAccess: false
}

interface CompatibleProvider {
  provider_id: string
  display_name: string
  capabilities?: { capability_projections?: string[] }
}

interface Props {
  connections: McpConnectionSummary[]
  locked: boolean
  providers: CompatibleProvider[]
  restartPending: boolean
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  onSettingsChanged: (settings: Record<string, unknown>) => void
  onRestartRequired: () => void
  onEditorStateChange?: (state: SettingsEditorState) => void
}

interface Draft {
  id: string
  name: string
  enabled: boolean
  transport: 'stdio' | 'http'
  providerIds: string[]
  command: string
  argumentsText: string
  cwd: string
  url: string
  bearerTokenEnvVar: string
  environmentText: string
  environmentKeys: string[]
}

const EMPTY_DRAFT: Draft = {
  id: '',
  name: '',
  enabled: false,
  transport: 'stdio',
  providerIds: [],
  command: '',
  argumentsText: '',
  cwd: '',
  url: '',
  bearerTokenEnvVar: '',
  environmentText: '',
  environmentKeys: [],
}

function draftFrom(connection: McpConnectionSummary): Draft {
  return {
    id: connection.id,
    name: connection.name,
    enabled: connection.enabled,
    transport: connection.transport,
    providerIds: [...connection.providerIds],
    command: connection.command,
    argumentsText: connection.arguments.join('\n'),
    cwd: connection.cwd,
    url: connection.url,
    bearerTokenEnvVar: connection.bearerTokenEnvVar,
    environmentText: '',
    environmentKeys: [...connection.environmentKeys],
  }
}

function parseEnvironment(text: string): Record<string, string | null> {
  const result: Record<string, string | null> = {}
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line) continue
    const separator = line.indexOf('=')
    if (separator <= 0) throw new Error(`Environment entry must use KEY=value: ${line}`)
    const key = line.slice(0, separator).trim()
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key)) throw new Error(`Invalid environment key: ${key}`)
    const value = line.slice(separator + 1)
    result[key] = value || null
  }
  return result
}

function endpointLabel(connection: McpConnectionSummary): string {
  if (connection.transport === 'http') return connection.url
  return [connection.command, ...connection.arguments].filter(Boolean).join(' ')
}

export default function McpConnections({
  connections,
  locked,
  providers,
  restartPending,
  send,
  onSettingsChanged,
  onRestartRequired,
  onEditorStateChange,
}: Props) {
  const { t } = useI18n()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [removeCandidate, setRemoveCandidate] = useState('')
  const [testState, setTestState] = useState<Record<string, { message: string; ok: boolean }>>({})
  const initialDraft = useRef('')
  const dirty = draft !== null && JSON.stringify(draft) !== initialDraft.current
  useEffect(() => {
    onEditorStateChange?.({ dirty, busy: saving })
    return () => onEditorStateChange?.({ dirty: false, busy: false })
  }, [dirty, saving, onEditorStateChange])
  const canLeave = () => canLeaveSettingsEditors([{ dirty, busy: saving }], () => window.confirm(t(DISCARD_CONNECTION_CHANGES)))
  const beginEdit = (next: Draft) => {
    if (!canLeave()) return
    initialDraft.current = JSON.stringify(next)
    setDraft(next)
    setError('')
    setRemoveCandidate('')
  }
  const closeEditor = () => { if (canLeave()) { setDraft(null); setError('') } }
  const compatibleProviders = useMemo(() => providers.filter(provider =>
    (provider.capabilities?.capability_projections || []).includes('mcp_connection'),
  ), [providers])

  const updateDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft(current => current ? { ...current, [key]: value } : current)
  }

  const save = async () => {
    if (!draft || !window.amadeus) return
    setSaving(true)
    setError('')
    try {
      const environment = parseEnvironment(draft.environmentText)
      const result = await window.amadeus.upsertMcpConnection({
        connection: {
          id: draft.id || undefined,
          name: draft.name,
          enabled: draft.enabled,
          transport: draft.transport,
          providerIds: draft.providerIds,
          command: draft.command,
          arguments: draft.argumentsText.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
          cwd: draft.cwd,
          url: draft.url,
          bearerTokenEnvVar: draft.bearerTokenEnvVar,
        },
        environment,
      })
      if (!result.ok) throw new Error(result.error || 'Could not save MCP connection')
      if (result.settings) onSettingsChanged(result.settings)
      onRestartRequired()
      setDraft(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not save MCP connection')
    } finally {
      setSaving(false)
    }
  }

  const remove = async (connectionId: string) => {
    if (!window.amadeus) return
    setSaving(true)
    setError('')
    try {
      const result = await window.amadeus.removeMcpConnection(connectionId)
      if (!result.ok) throw new Error(result.error || 'Could not remove MCP connection')
      if (result.settings) onSettingsChanged(result.settings)
      onRestartRequired()
      setRemoveCandidate('')
      if (draft?.id === connectionId) setDraft(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not remove MCP connection')
    } finally {
      setSaving(false)
    }
  }

  const test = async (connectionId: string) => {
    setTestState(current => ({ ...current, [connectionId]: { message: t('Connecting…'), ok: false } }))
    try {
      const result = await send('mcp.connection.test', { connection_id: connectionId })
      const detail = result.status === 'connected'
        ? t('Connected · {count} tools discovered', { count: Number(result.tool_count || 0) })
        : t(String(result.detail || result.code || 'Connection failed'))
      setTestState(current => ({ ...current, [connectionId]: { message: detail, ok: result.status === 'connected' } }))
    } catch (reason) {
      setTestState(current => ({
        ...current,
        [connectionId]: { message: reason instanceof Error ? reason.message : t('Connection failed'), ok: false },
      }))
    }
  }

  return (
    <div className="settings-embedded-stack">
      <div className="rounded-lg" style={{ padding: '10px 12px', border: '1px solid var(--border)', background: 'var(--focus-fill)' }}>
        <div className="settings-note-title">{t('Provider-only boundary')}</div>
        <div className="settings-note-description">
          {t('MCP connections are available only to compatible Work Providers. Main Chat cannot access MCP tools.')}
        </div>
      </div>

      {connections.map(connection => (
        <div key={connection.id} className="setting-card" style={{ background: 'var(--surface)', border: '1px solid var(--card-border)', borderRadius: 11, padding: 12, boxShadow: '0 1px 2px color-mix(in srgb, var(--shadow-color) 25%, transparent)' }}>
          <div className="flex items-start gap-3">
            <div className="flex items-center justify-center mt-0.5" style={{ width: 24, color: 'var(--muted)' }}><FluentIcon name="CommandPrompt" size={17} /></div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="settings-card-title">{connection.name}</span>
                <span className="text-[9px] uppercase tracking-wide" style={{ color: 'var(--muted)' }}>{connection.transport}</span>
              </div>
              <div className="settings-card-description truncate">{endpointLabel(connection)}</div>
            </div>
            <StatusPill ok={false} tone={restartPending ? 'warning' : 'neutral'}>{t(restartPending ? 'Restart required' : connection.enabled ? 'Enabled on startup' : 'Disabled')}</StatusPill>
          </div>
          <div className="settings-connection-facts" style={{ borderTop: '1px solid var(--divider)', color: 'var(--muted)' }}>
            <div><span className="font-[600]">{t('Providers')}:</span> {connection.providerIds.length ? connection.providerIds.join(', ') : t('Not bound')}</div>
            <div><span className="font-[600]">{t('Main Chat')}:</span> {t('No access')}</div>
            <div className="col-span-2"><span className="font-[600]">{t('Encrypted environment')}:</span> {connection.environmentKeys.length ? connection.environmentKeys.join(', ') : t('None')}</div>
          </div>
          {testState[connection.id] ? <div role="status" className="settings-field-description" style={{ color: testState[connection.id].ok ? 'var(--success)' : 'var(--muted)' }}>{testState[connection.id].message}</div> : null}
          <div className="settings-form-actions-end">
            <SettingsButton tone="quiet" onClick={() => void test(connection.id)} disabled={restartPending || saving} title={t(restartPending ? 'Restart the backend before testing' : 'Connect and discover tools')}>{t('Test')}</SettingsButton>
            <SettingsButton onClick={() => beginEdit(draftFrom(connection))} disabled={locked || saving}>{t('Edit')}</SettingsButton>
            <SettingsButton tone="danger" onClick={() => removeCandidate === connection.id ? void remove(connection.id) : setRemoveCandidate(connection.id)} disabled={locked || saving}>{t(removeCandidate === connection.id ? 'Confirm remove' : 'Remove')}</SettingsButton>
            {removeCandidate === connection.id ? <SettingsButton tone="quiet" disabled={saving} onClick={() => setRemoveCandidate('')}>{t('Keep connection')}</SettingsButton> : null}
          </div>
        </div>
      ))}

      {draft ? (
        <div className="setting-card settings-connection-editor">
          <div className="settings-form-section-heading settings-connection-editor-heading">
            <div><h4 className="settings-card-title">{t(draft.id ? 'Edit MCP connection' : 'Add MCP server')}</h4>
              <p className="settings-card-description">{t('Saved by the Host and applied to selected Work Providers after restart.')}</p></div>
            <SettingsButton tone="quiet" disabled={saving} onClick={closeEditor} aria-label={t('Close MCP editor')}>×</SettingsButton>
          </div>
          <div className="settings-form">
            <fieldset disabled={locked || saving} className="settings-form-body">
              <section className="settings-form-section" aria-label={t('Connection details')}>
                <SettingsField id="mcp-name" label="Name"><input id="mcp-name" className="settings-form-input" value={draft.name} onChange={event => updateDraft('name', event.target.value)} placeholder="GitHub" /></SettingsField>
                <SettingsField id="mcp-transport" label="Transport"><select id="mcp-transport" className="settings-form-input" value={draft.transport} onChange={event => updateDraft('transport', event.target.value as 'stdio' | 'http')}><option value="stdio">{t('Local command (stdio)')}</option><option value="http">Streamable HTTP</option></select></SettingsField>
                {draft.transport === 'stdio' ? <>
                  <SettingsField id="mcp-command" label="Command"><input id="mcp-command" className="settings-form-input settings-form-code" value={draft.command} onChange={event => updateDraft('command', event.target.value)} placeholder="npx" /></SettingsField>
                  <SettingsField id="mcp-arguments" label="Arguments — one per line"><textarea id="mcp-arguments" className="settings-form-input settings-form-code" rows={3} value={draft.argumentsText} onChange={event => updateDraft('argumentsText', event.target.value)} placeholder={'-y\n@modelcontextprotocol/server-filesystem'} /></SettingsField>
                  <SettingsField id="mcp-directory" label="Working directory" description="Optional."><input id="mcp-directory" aria-describedby="mcp-directory-hint" className="settings-form-input settings-form-code" value={draft.cwd} onChange={event => updateDraft('cwd', event.target.value)} /></SettingsField>
                </> : <>
                  <SettingsField id="mcp-url" label="Server URL"><input id="mcp-url" type="url" className="settings-form-input settings-form-code" value={draft.url} onChange={event => updateDraft('url', event.target.value)} placeholder="https://example.com/mcp" /></SettingsField>
                  <SettingsField id="mcp-token" label="Bearer token environment variable" description="Enter a variable name, not a token value."><input id="mcp-token" aria-describedby="mcp-token-hint" className="settings-form-input settings-form-code" value={draft.bearerTokenEnvVar} onChange={event => updateDraft('bearerTokenEnvVar', event.target.value)} placeholder="MCP_TOKEN" /></SettingsField>
                </>}
              </section>
              <details className="settings-form-advanced">
                <summary><span>{t('Encrypted environment values')}</span><span className="settings-meta-text">{t('Optional')}</span></summary>
                <div className="settings-form-advanced-body">
                  <SettingsField id="mcp-environment" label="Environment values" description="Leave blank to keep stored values; use KEY= to remove one. Values are stored encrypted by the Host.">
                    <textarea id="mcp-environment" aria-describedby="mcp-environment-hint" className="settings-form-input settings-form-code" rows={3} value={draft.environmentText} onChange={event => updateDraft('environmentText', event.target.value)} placeholder={t('One KEY=value per line')} autoComplete="off" spellCheck={false} />
                  </SettingsField>
                  {draft.environmentKeys.length ? <div className="settings-meta-text">{t('Stored variables')}: {draft.environmentKeys.join(', ')}</div> : null}
                </div>
              </details>
              <section className="settings-form-section" aria-labelledby="mcp-providers-label">
                <h4 id="mcp-providers-label" className="settings-card-title">{t('Compatible Work Providers')}</h4>
                <p className="settings-field-description">{t('Saving a connection does not grant it to Main Chat.')}</p>
                <div role="group" aria-labelledby="mcp-providers-label" className="settings-form-provider-choices">
                  {compatibleProviders.length ? compatibleProviders.map(provider => {
                    const checked = draft.providerIds.includes(provider.provider_id)
                    return <label key={provider.provider_id}><input type="checkbox" checked={checked} onChange={() => updateDraft('providerIds', checked ? draft.providerIds.filter(value => value !== provider.provider_id) : [...draft.providerIds, provider.provider_id])} />{provider.display_name || provider.provider_id}</label>
                  }) : <p className="settings-field-description">{t('No installed Work Provider currently accepts MCP connections.')}</p>}
                </div>
                <SettingsField id="mcp-enabled" label="Enable for selected Providers" description="Applies after saving and restarting the backend.">
                  <div className="settings-form-switch"><input id="mcp-enabled" type="checkbox" role="switch" aria-describedby="mcp-enabled-hint" checked={draft.enabled} onChange={event => updateDraft('enabled', event.target.checked)} /><span>{t(draft.enabled ? 'On' : 'Off')}</span></div>
                </SettingsField>
              </section>
            </fieldset>
            <div className="settings-form-footer">
              {error ? <div role="alert" className="settings-form-error">{t(error)}</div> : null}
              <div className="settings-form-save-hint" role="status">{t(dirty ? 'Unsaved changes' : 'Changes are saved together. Restart the backend to apply.')}</div>
              <div className="settings-form-actions-end"><SettingsButton tone="quiet" disabled={saving} onClick={closeEditor}>{t('Cancel')}</SettingsButton><SettingsButton tone="primary" onClick={() => void save()} disabled={locked || saving || Boolean(draft.id) && !dirty}>{t(saving ? 'Saving…' : 'Save connection')}</SettingsButton></div>
            </div>
          </div>
        </div>
      ) : (
        <SettingsButton className="self-start" onClick={() => beginEdit({ ...EMPTY_DRAFT })} disabled={locked}>+ {t('Add MCP server')}</SettingsButton>
      )}
      {locked ? <div className="text-[10.5px]" style={{ color: 'var(--muted)' }}>{t('MCP registry is locked by the parent process environment.')}</div> : null}
      {!connections.length && !draft ? <div className="text-[10.5px]" style={{ color: 'var(--muted)' }}>{t('No MCP connections configured.')}</div> : null}
      {error && !draft ? <div role="alert" className="settings-form-error">{t(error)}</div> : null}
    </div>
  )
}
