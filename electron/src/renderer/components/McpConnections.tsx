import { useMemo, useState } from 'react'
import FluentIcon from './FluentIcon'

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

function FieldLabel({ children }: { children: string }) {
  return <label className="text-[11.5px] font-[600]" style={{ color: 'var(--text)', lineHeight: '17px' }}>{children}</label>
}

const inputClass = 'w-full text-[12px] bg-white border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)]'

export default function McpConnections({
  connections,
  locked,
  providers,
  restartPending,
  send,
  onSettingsChanged,
  onRestartRequired,
}: Props) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [removeCandidate, setRemoveCandidate] = useState('')
  const [testState, setTestState] = useState<Record<string, string>>({})
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
    setTestState(current => ({ ...current, [connectionId]: 'Connecting…' }))
    try {
      const result = await send('mcp.connection.test', { connection_id: connectionId })
      const detail = result.status === 'connected'
        ? `Connected · ${Number(result.tool_count || 0)} tools discovered`
        : String(result.detail || result.code || 'Connection failed')
      setTestState(current => ({ ...current, [connectionId]: detail }))
    } catch (reason) {
      setTestState(current => ({
        ...current,
        [connectionId]: reason instanceof Error ? reason.message : 'Connection failed',
      }))
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="rounded-lg" style={{ padding: '10px 12px', border: '1px solid rgba(0,120,212,0.18)', background: 'rgba(0,120,212,0.035)' }}>
        <div className="text-[11.5px] font-[650]" style={{ color: 'var(--text)', lineHeight: '16px' }}>Provider-only boundary</div>
        <div className="text-[10.5px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '15px' }}>
          MCP connections are available only to compatible Work Providers. Main Chat cannot access MCP tools.
        </div>
      </div>

      {connections.map(connection => (
        <div key={connection.id} className="setting-card" style={{ background: 'var(--surface)', border: '1px solid rgba(17,24,39,0.085)', borderRadius: 11, padding: '12px 14px', boxShadow: '0 1px 2px rgba(17,24,39,0.025)' }}>
          <div className="flex items-start gap-3">
            <div className="flex items-center justify-center mt-0.5" style={{ width: 24, color: 'var(--muted)' }}><FluentIcon name="CommandPrompt" size={17} /></div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-[650]" style={{ color: 'var(--text)' }}>{connection.name}</span>
                <span className="text-[9px] uppercase tracking-wide" style={{ color: 'var(--muted)' }}>{connection.transport}</span>
              </div>
              <div className="text-[10.5px] mt-0.5 truncate" style={{ color: 'var(--muted)', lineHeight: '15px' }}>{endpointLabel(connection)}</div>
            </div>
            <span className="text-[10px] font-[700] rounded-full px-2.5 py-1" style={{ color: connection.enabled ? '#107C10' : '#605E5C', background: connection.enabled ? '#E8F5E9' : '#F2F2F2' }}>{connection.enabled ? 'Enabled' : 'Disabled'}</span>
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 mt-2.5 pt-2.5 text-[10px]" style={{ borderTop: '1px solid rgba(17,24,39,0.075)', color: 'var(--muted)' }}>
            <div><span className="font-[600]">Providers:</span> {connection.providerIds.length ? connection.providerIds.join(', ') : 'Not bound'}</div>
            <div><span className="font-[600]">Main Chat:</span> No access</div>
            <div className="col-span-2"><span className="font-[600]">Encrypted environment:</span> {connection.environmentKeys.length ? connection.environmentKeys.join(', ') : 'None'}</div>
          </div>
          {testState[connection.id] ? <div className="text-[10.5px] mt-2" style={{ color: testState[connection.id].startsWith('Connected') ? '#107C10' : 'var(--muted)' }}>{testState[connection.id]}</div> : null}
          <div className="flex items-center justify-end gap-1 mt-2">
            <button onClick={() => void test(connection.id)} disabled={restartPending || saving} className="text-[10.5px] rounded-md px-2.5 disabled:opacity-35" style={{ height: 28, color: 'var(--muted)', background: 'transparent', border: 0 }} title={restartPending ? 'Restart the backend before testing' : 'Connect and discover tools'}>Test</button>
            <button onClick={() => { setDraft(draftFrom(connection)); setError('') }} disabled={locked || saving} className="text-[10.5px] rounded-md px-2.5 disabled:opacity-35" style={{ height: 28, color: 'var(--text)', background: 'rgba(17,24,39,0.045)', border: 0 }}>Edit</button>
            <button onClick={() => removeCandidate === connection.id ? void remove(connection.id) : setRemoveCandidate(connection.id)} disabled={locked || saving} className="text-[10.5px] rounded-md px-2.5 disabled:opacity-35" style={{ height: 28, color: removeCandidate === connection.id ? '#b42318' : 'var(--muted)', background: 'transparent', border: 0 }}>{removeCandidate === connection.id ? 'Confirm remove' : 'Remove'}</button>
          </div>
        </div>
      ))}

      {draft ? (
        <div className="setting-card" style={{ background: 'var(--surface)', border: '1px solid rgba(0,120,212,0.28)', borderRadius: 11, padding: '14px', boxShadow: '0 1px 2px rgba(17,24,39,0.025)' }}>
          <div className="flex items-start justify-between gap-4 mb-4">
            <div>
              <div className="text-[13px] font-[650]" style={{ color: 'var(--text)' }}>{draft.id ? 'Edit MCP connection' : 'Add MCP server'}</div>
              <div className="text-[10.5px] mt-0.5" style={{ color: 'var(--muted)' }}>Saved by the Host and applied to selected Work Providers after restart.</div>
            </div>
            <button onClick={() => setDraft(null)} className="text-[18px]" style={{ color: 'var(--muted)', background: 'transparent', border: 0, lineHeight: 1 }} aria-label="Close MCP editor">×</button>
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-3">
            <div className="flex flex-col gap-1"><FieldLabel>Name</FieldLabel><input className={inputClass} style={{ height: 34 }} value={draft.name} onChange={event => updateDraft('name', event.target.value)} placeholder="GitHub" /></div>
            <div className="flex flex-col gap-1"><FieldLabel>Transport</FieldLabel><select className={inputClass} style={{ height: 34 }} value={draft.transport} onChange={event => updateDraft('transport', event.target.value as 'stdio' | 'http')}><option value="stdio">stdio command</option><option value="http">Streamable HTTP</option></select></div>
            {draft.transport === 'stdio' ? (
              <>
                <div className="col-span-2 flex flex-col gap-1"><FieldLabel>Command</FieldLabel><input className={inputClass} style={{ height: 34 }} value={draft.command} onChange={event => updateDraft('command', event.target.value)} placeholder="npx" /></div>
                <div className="flex flex-col gap-1"><FieldLabel>Arguments</FieldLabel><textarea className={inputClass} style={{ minHeight: 76, paddingTop: 8, resize: 'vertical' }} value={draft.argumentsText} onChange={event => updateDraft('argumentsText', event.target.value)} placeholder={'-y\n@modelcontextprotocol/server-filesystem'} /></div>
                <div className="flex flex-col gap-1"><FieldLabel>Working directory</FieldLabel><input className={inputClass} style={{ height: 34 }} value={draft.cwd} onChange={event => updateDraft('cwd', event.target.value)} placeholder="Optional" /></div>
              </>
            ) : (
              <>
                <div className="col-span-2 flex flex-col gap-1"><FieldLabel>Server URL</FieldLabel><input type="url" className={inputClass} style={{ height: 34 }} value={draft.url} onChange={event => updateDraft('url', event.target.value)} placeholder="https://example.com/mcp" /></div>
                <div className="col-span-2 flex flex-col gap-1"><FieldLabel>Bearer token environment variable</FieldLabel><input className={inputClass} style={{ height: 34 }} value={draft.bearerTokenEnvVar} onChange={event => updateDraft('bearerTokenEnvVar', event.target.value)} placeholder="Optional · for example MCP_TOKEN" /></div>
              </>
            )}
            <div className="col-span-2 flex flex-col gap-1">
              <FieldLabel>Encrypted environment values</FieldLabel>
              <textarea className={inputClass} style={{ minHeight: 70, paddingTop: 8, resize: 'vertical' }} value={draft.environmentText} onChange={event => updateDraft('environmentText', event.target.value)} placeholder={draft.environmentKeys.length ? 'Leave blank to keep stored values; use KEY= to remove one' : 'Optional · one KEY=value per line'} />
              {draft.environmentKeys.length ? <div className="text-[10px]" style={{ color: 'var(--muted)' }}>Stored: {draft.environmentKeys.join(', ')}</div> : null}
            </div>
            <div className="col-span-2 flex flex-col gap-1.5">
              <FieldLabel>Compatible Work Providers</FieldLabel>
              {compatibleProviders.length ? compatibleProviders.map(provider => {
                const checked = draft.providerIds.includes(provider.provider_id)
                return <label key={provider.provider_id} className="flex items-center gap-2 text-[11px]" style={{ color: 'var(--text)' }}><input type="checkbox" checked={checked} onChange={() => updateDraft('providerIds', checked ? draft.providerIds.filter(value => value !== provider.provider_id) : [...draft.providerIds, provider.provider_id])} />{provider.display_name || provider.provider_id}</label>
              }) : <div className="text-[10.5px]" style={{ color: '#8A5414' }}>No installed Work Provider currently accepts MCP connections.</div>}
            </div>
            <label className="col-span-2 flex items-start gap-2 text-[11px]" style={{ color: 'var(--text)' }}><input type="checkbox" checked={draft.enabled} onChange={event => updateDraft('enabled', event.target.checked)} style={{ marginTop: 2 }} /><span><span className="font-[600]">Enable for selected Providers</span><span className="block text-[10px] mt-0.5" style={{ color: 'var(--muted)' }}>Saving a connection does not grant it to Main Chat.</span></span></label>
          </div>
          {error ? <div className="text-[10.5px] mt-3" style={{ color: '#b42318' }}>{error}</div> : null}
          <div className="flex justify-end gap-2 mt-4"><button onClick={() => setDraft(null)} className="text-[11px] rounded-md px-3" style={{ height: 31, color: 'var(--muted)', background: 'transparent', border: 0 }}>Cancel</button><button onClick={() => void save()} disabled={saving} className="text-[11px] font-[600] rounded-md px-3 disabled:opacity-50" style={{ height: 31, color: 'white', background: 'var(--accent)', border: 0 }}>{saving ? 'Saving…' : 'Save connection'}</button></div>
        </div>
      ) : (
        <button onClick={() => { setDraft({ ...EMPTY_DRAFT }); setError('') }} disabled={locked} className="self-start text-[11px] font-[600] rounded-md px-3 disabled:opacity-40" style={{ height: 32, color: 'var(--text)', background: 'rgba(17,24,39,0.055)', border: '1px solid rgba(17,24,39,0.07)' }}>+ Add MCP server</button>
      )}
      {locked ? <div className="text-[10.5px]" style={{ color: 'var(--muted)' }}>MCP registry is locked by the parent process environment.</div> : null}
      {!connections.length && !draft ? <div className="text-[10.5px]" style={{ color: 'var(--muted)' }}>No MCP connections configured.</div> : null}
      {error && !draft ? <div className="text-[10.5px]" style={{ color: '#b42318' }}>{error}</div> : null}
    </div>
  )
}
