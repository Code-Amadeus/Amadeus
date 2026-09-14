import { useEffect, useState } from 'react'

type Profile = {
  id: string; name: string; command: string; args: string[]; enabled: boolean; resume: boolean
  environment: Record<string, string>; config_options: Record<string, string>
}
type Option = { id: string; name: string; type: string; currentValue: string; options?: Array<{ value?: string; name: string; options?: Array<{ value: string; name: string }> }> }
export type AcpConfiguration = { provider_id: string; config_options?: Option[] }
const inputStyle = 'w-full text-xs border border-[var(--border)] rounded-md p-2 bg-white'
const controlStyle = { padding: '7px 9px', fontSize: 12, minHeight: 34, marginTop: 4 }
const buttonStyle = { padding: '6px 10px', fontSize: 12, border: '1px solid var(--border)', borderRadius: 6, background: 'var(--surface)', cursor: 'pointer' }
const providerEnvironmentNames = { deepseek: 'DEEPSEEK_API_KEY', claude: 'ANTHROPIC_API_KEY' }

function pairs(text: string): Record<string, string> {
  const entries = text.split('\n').filter(line => line.trim()).map(line => {
    const at = line.indexOf('=')
    if (at < 1) throw new Error('Use one name=value entry per line')
    return [line.slice(0, at).trim(), line.slice(at + 1).trim()]
  })
  if (new Set(entries.map(([name]) => name)).size !== entries.length) throw new Error('Duplicate configuration name')
  return Object.fromEntries(entries)
}

export default function AcpProviders({ encoded, locked, configurations, onSave, onRefresh }: {
  encoded: string; locked: boolean; configurations: AcpConfiguration[]
  onSave: (encoded: string) => Promise<void>; onRefresh: () => Promise<void>
}) {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [selected, setSelected] = useState(-1)
  const [draft, setDraft] = useState<Profile | null>(null)
  const [environment, setEnvironment] = useState('')
  const [options, setOptions] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    try {
      const parsed = JSON.parse(encoded || '[]')
      if (!Array.isArray(parsed)) throw new Error('Invalid ACP agent configuration')
      setProfiles(parsed)
    } catch { setError('The saved ACP configuration is invalid. Correct the configured environment value before enabling agents.') }
  }, [encoded])
  const edit = (profile: Profile, index: number) => {
    setSelected(index)
    setDraft({ ...profile, args: profile.args || [], environment: profile.environment || {}, config_options: profile.config_options || {} })
    setEnvironment(Object.entries(profile.environment || {}).map(([k, v]) => `${k}=${v}`).join('\n'))
    setOptions(Object.entries(profile.config_options || {}).map(([k, v]) => `${k}=${v}`).join('\n'))
    setError('')
  }
  const add = (kind: 'deepseek' | 'claude' | 'custom') => edit({
    id: kind === 'custom' ? '' : kind, name: kind === 'deepseek' ? 'DeepSeek Harness' : kind === 'claude' ? 'Claude' : '',
    command: 'node', args: [], enabled: false, resume: kind !== 'custom', config_options: {},
    environment: kind === 'custom' ? {} : { [providerEnvironmentNames[kind]]: providerEnvironmentNames[kind] },
  }, -1)
  const save = async (next: Profile[]) => {
    setBusy(true); setError('')
    try { await onSave(JSON.stringify(next)); setProfiles(next); setDraft(null) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save ACP agents') }
    finally { setBusy(false) }
  }
  const known = configurations.find(item => item.provider_id === draft?.id)?.config_options || []
  return <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 12, lineHeight: '18px' }}>
    <p className="text-[var(--muted)]">Connect an installed agent. Save and restart the backend to apply changes. Each agent keeps its own model, tools and native permissions.</p>
    {profiles.map((profile, index) => <div key={profile.id} className="flex items-center justify-between border border-[var(--border)] rounded-lg" style={{ padding: 12 }}>
      <span>{profile.name || profile.id} · {profile.enabled ? 'Enabled' : 'Disabled'}</span>
      <div className="flex gap-3">
        <button style={buttonStyle} disabled={locked || busy} onClick={() => edit(profile, index)}>Edit</button>
        <button style={buttonStyle} disabled={locked || busy} onClick={() => void save(profiles.filter((_, i) => i !== index))}>Remove</button>
      </div>
    </div>)}
    <div className="flex gap-3">
      <button style={buttonStyle} disabled={locked || busy} onClick={() => add('deepseek')}>Add DeepSeek</button>
      <button style={buttonStyle} disabled={locked || busy} onClick={() => add('claude')}>Add Claude</button>
      <button style={buttonStyle} disabled={locked || busy} onClick={() => add('custom')}>Add custom agent</button>
    </div>
    {draft && <div className="border border-[var(--border)] rounded-lg" style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="grid grid-cols-2 gap-3">
        <label>Agent id<input className={inputStyle} style={controlStyle} value={draft.id} disabled={selected >= 0} onChange={e => setDraft({ ...draft, id: e.target.value })}/></label>
        <label>Display name<input className={inputStyle} style={controlStyle} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })}/></label>
      </div>
      <label className="block">Executable<input className={inputStyle} style={controlStyle} value={draft.command} onChange={e => setDraft({ ...draft, command: e.target.value })}/></label>
      <label className="block">Arguments — one per line<textarea className={inputStyle} style={controlStyle} rows={3} value={draft.args.join('\n')} onChange={e => setDraft({ ...draft, args: e.target.value.split('\n') })} placeholder={'C:\\path\\to\\installed-agent\\cli.js\n--profile\nacp'}/></label>
      <p className="text-[var(--muted)]">On Windows, use node.exe and the installed agent's JavaScript entry file. Commands are launched directly; shell scripts and automatic package installation are not used.</p>
      <label className="block">Environment references — child variable=Host variable<textarea className={inputStyle} style={controlStyle} rows={2} value={environment} onChange={e => setEnvironment(e.target.value)}/></label>
      <p className="text-[var(--muted)]">Reference API keys saved in Settings or supplied in the backend environment. Enter variable names here, not secret values. Omit the reference when using an agent's existing login.</p>
      <div className="flex justify-between"><span>Model and agent options</span><button style={buttonStyle} onClick={() => void onRefresh().catch(e => setError(String(e)))}>Refresh available choices</button></div>
      {known.length === 0 && <p className="text-[var(--muted)]">Choices become available after this agent opens its first task. Leave overrides empty to use the agent's defaults.</p>}
      {known.filter(option => option.type === 'select').map(option => {
        let selectedValue = ''
        try { selectedValue = pairs(options)[option.id] || '' } catch { /* preserve an unfinished advanced entry */ }
        return <label className="block" key={option.id}>{option.name}<select className={inputStyle} style={controlStyle} value={selectedValue} onChange={e => {
          try {
            const next = pairs(options)
            if (e.target.value) next[option.id] = e.target.value; else delete next[option.id]
            setOptions(Object.entries(next).map(([k, v]) => `${k}=${v}`).join('\n'))
          } catch (reason) { setError(String(reason)) }
        }}>
          <option value="">Agent default ({option.currentValue})</option>
          {(option.options || []).flatMap(item => item.options || (item.value ? [{ value: item.value, name: item.name }] : [])).map(item => <option key={item.value} value={item.value}>{item.name}</option>)}
        </select></label>
      })}
      <details><summary>Explicit option overrides</summary><textarea className={inputStyle} style={controlStyle} value={options} rows={2} placeholder="model=agent-model-id" onChange={e => setOptions(e.target.value)}/></details>
      <div className="flex gap-5">
        <label><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })}/> Enable agent</label>
        <label><input type="checkbox" checked={draft.resume} onChange={e => setDraft({ ...draft, resume: e.target.checked })}/> Reuse persistent native sessions</label>
      </div>
      <div className="flex gap-3">
        <button style={buttonStyle} disabled={locked || busy} onClick={() => {
          try {
            const updated = { ...draft, environment: pairs(environment), config_options: pairs(options) }
            void save(selected < 0 ? [...profiles, updated] : profiles.map((item, index) => index === selected ? updated : item))
          } catch (reason) { setError(String(reason)) }
        }}>{busy ? 'Saving…' : 'Save agent'}</button>
        <button style={buttonStyle} disabled={busy} onClick={() => setDraft(null)}>Cancel</button>
      </div>
    </div>}
    {locked && <p>Agent configuration is controlled by the parent process environment.</p>}
    {error && <p role="alert" className="text-red-700">{error}</p>}
  </div>
}
