import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'
import FluentIcon from './FluentIcon'

type Profile = {
  id: string; name: string; command: string; args: string[]; enabled: boolean; resume: boolean
  environment: Record<string, string>; config_options: Record<string, string>
}
type Option = { id: string; name: string; type: string; currentValue: string; options?: Array<{ value?: string; name: string; options?: Array<{ value: string; name: string }> }> }
export type AcpConfiguration = { provider_id: string; config_options?: Option[] }
const inputStyle = 'w-full text-xs text-[var(--text)] border border-[var(--border)] rounded-md p-2 bg-[var(--surface-alt)]'
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

export default function AcpProviders({ encoded, locked, electronUnavailable = false, configurations, onSave, onRefresh }: {
  encoded: string; locked: boolean; electronUnavailable?: boolean; configurations: AcpConfiguration[]
  onSave: (encoded: string) => Promise<void>; onRefresh: () => Promise<void>
}) {
  const { t } = useI18n()
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [selected, setSelected] = useState(-1)
  const [editorKey, setEditorKey] = useState('')
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
  const edit = (profile: Profile, index: number, key: string) => {
    setSelected(index)
    setEditorKey(key)
    setDraft({ ...profile, args: profile.args || [], environment: profile.environment || {}, config_options: profile.config_options || {} })
    setEnvironment(Object.entries(profile.environment || {}).map(([k, v]) => `${k}=${v}`).join('\n'))
    setOptions(Object.entries(profile.config_options || {}).map(([k, v]) => `${k}=${v}`).join('\n'))
    setError('')
  }
  const add = (kind: 'deepseek' | 'claude' | 'custom', key: string) => edit({
    id: kind === 'custom' ? '' : kind, name: kind === 'deepseek' ? 'DeepSeek Harness' : kind === 'claude' ? 'Claude' : '',
    command: 'node', args: [], enabled: false, resume: kind !== 'custom', config_options: {},
    environment: kind === 'custom' ? {} : { [providerEnvironmentNames[kind]]: providerEnvironmentNames[kind] },
  }, -1, key)
  const save = async (next: Profile[]) => {
    setBusy(true); setError('')
    try { await onSave(JSON.stringify(next)); setProfiles(next); setDraft(null); setEditorKey('') }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save ACP agents') }
    finally { setBusy(false) }
  }
  const known = configurations.find(item => item.provider_id === draft?.id)?.config_options || []
  const closeEditor = () => { setDraft(null); setEditorKey(''); setSelected(-1); setError('') }
  const presets = [
    { id: 'deepseek', name: 'DeepSeek Harness', kind: 'deepseek' as const, description: 'ACP v1 preset using a referenced DeepSeek credential.' },
    { id: 'claude', name: 'Claude', kind: 'claude' as const, description: 'ACP v1 preset using a referenced Anthropic credential.' },
  ]
  const configuredPresetIds = new Set(profiles.filter(profile => ['deepseek', 'claude'].includes(profile.id)).map(profile => profile.id))
  const cards: Array<{ key: string; profile?: Profile; index: number; kind?: 'deepseek' | 'claude' | 'custom'; title: string; description: string }> = []
  for (const preset of presets) {
    const index = profiles.findIndex(profile => profile.id === preset.id)
    if (index >= 0) {
      const profile = profiles[index]
      cards.push({ key: `profile-${index}`, profile, index, title: profile.name || profile.id, description: preset.description })
    } else {
      cards.push({ key: `preset-${preset.id}`, index: -1, kind: preset.kind, title: preset.name, description: preset.description })
    }
  }
  profiles.forEach((profile, index) => {
    if (!configuredPresetIds.has(profile.id) && !['deepseek', 'claude'].includes(profile.id)) {
      cards.push({ key: `profile-${index}`, profile, index, title: profile.name || profile.id, description: 'Custom ACP v1 agent connection.' })
    }
  })
  cards.push({ key: 'add-custom', index: -1, kind: 'custom', title: 'Add custom ACP agent', description: 'Register another installed ACP v1 command.' })

  const renderEditor = () => draft ? <div className="acp-agent-editor">
    <div className="grid grid-cols-2 gap-3">
      <label>{t('Agent id')}<input className={inputStyle} style={controlStyle} value={draft.id} disabled={locked || selected >= 0} onChange={e => setDraft({ ...draft, id: e.target.value })}/></label>
      <label>{t('Display name')}<input className={inputStyle} style={controlStyle} value={draft.name} disabled={locked} onChange={e => setDraft({ ...draft, name: e.target.value })}/></label>
    </div>
    <label className="block">{t('Executable')}<input className={inputStyle} style={controlStyle} value={draft.command} disabled={locked} onChange={e => setDraft({ ...draft, command: e.target.value })}/></label>
    <label className="block">{t('Arguments — one per line')}<textarea className={inputStyle} style={controlStyle} rows={3} value={draft.args.join('\n')} disabled={locked} onChange={e => setDraft({ ...draft, args: e.target.value.split('\n') })} placeholder={'C:\\path\\to\\installed-agent\\cli.js\n--profile\nacp'}/></label>
    <p className="settings-card-description">{t("On Windows, use node.exe and the installed agent's JavaScript entry file. Commands are launched directly; shell scripts and automatic package installation are not used.")}</p>
    <label className="block">{t('Environment references — child variable=Host variable')}<textarea className={inputStyle} style={controlStyle} rows={2} value={environment} disabled={locked} onChange={e => setEnvironment(e.target.value)}/></label>
    <p className="settings-card-description">{t("Reference API keys saved in Settings or supplied in the backend environment. Enter variable names here, not secret values. Omit the reference when using an agent's existing login.")}</p>
    <div className="flex justify-between items-center gap-3"><span>{t('Model and agent options')}</span><button style={buttonStyle} disabled={locked} onClick={() => void onRefresh().catch(e => setError(String(e)))}>{t('Refresh available choices')}</button></div>
    {known.length === 0 && <p className="settings-card-description">{t("Choices become available after this agent opens its first task. Leave overrides empty to use the agent's defaults.")}</p>}
    {known.filter(option => option.type === 'select').map(option => {
      let selectedValue = ''
      try { selectedValue = pairs(options)[option.id] || '' } catch { /* preserve an unfinished advanced entry */ }
      return <label className="block" key={option.id}>{option.name}<select className={inputStyle} style={controlStyle} value={selectedValue} disabled={locked} onChange={e => {
        try {
          const next = pairs(options)
          if (e.target.value) next[option.id] = e.target.value; else delete next[option.id]
          setOptions(Object.entries(next).map(([k, v]) => `${k}=${v}`).join('\n'))
        } catch (reason) { setError(String(reason)) }
      }}>
        <option value="">{t('Agent default')} ({option.currentValue})</option>
        {(option.options || []).flatMap(item => item.options || (item.value ? [{ value: item.value, name: item.name }] : [])).map(item => <option key={item.value} value={item.value}>{item.name}</option>)}
      </select></label>
    })}
    <details><summary>{t('Explicit option overrides')}</summary><textarea className={inputStyle} style={controlStyle} value={options} rows={2} disabled={locked} placeholder="model=agent-model-id" onChange={e => setOptions(e.target.value)}/></details>
    <div className="flex gap-5 flex-wrap">
      <label><input type="checkbox" checked={draft.enabled} disabled={locked} onChange={e => setDraft({ ...draft, enabled: e.target.checked })}/> {t('Enable agent')}</label>
      <label><input type="checkbox" checked={draft.resume} disabled={locked} onChange={e => setDraft({ ...draft, resume: e.target.checked })}/> {t('Reuse persistent native sessions')}</label>
    </div>
    <div className="flex items-center gap-3 flex-wrap">
      <button style={buttonStyle} disabled={locked || busy} onClick={() => {
        try {
          const updated = { ...draft, environment: pairs(environment), config_options: pairs(options) }
          void save(selected < 0 ? [...profiles, updated] : profiles.map((item, index) => index === selected ? updated : item))
        } catch (reason) { setError(String(reason)) }
      }}>{t(busy ? 'Saving…' : 'Save agent')}</button>
      {selected >= 0 ? <button style={buttonStyle} disabled={locked || busy} onClick={() => void save(profiles.filter((_, index) => index !== selected))}>{t('Remove')}</button> : null}
      <button style={buttonStyle} disabled={busy} onClick={closeEditor}>{t('Cancel')}</button>
    </div>
  </div> : null

  return <div className="settings-embedded-stack acp-settings-stack">
    <p className="settings-card-description">{t('Connect an installed agent. Save and restart the backend to apply changes. Each agent keeps its own model, tools and native permissions.')}</p>
    {cards.map(card => {
      const open = editorKey === card.key
      const status = card.profile ? (card.profile.enabled ? 'Enabled' : 'Disabled') : card.kind === 'custom' ? 'Add' : 'Optional'
      const tone = card.profile?.enabled ? 'success' : 'neutral'
      const model = card.profile?.config_options?.model
      const command = card.profile ? [card.profile.command, ...(card.profile.args || [])].filter(Boolean).join(' ') : ''
      const detail = model ? `${command} · ${model}` : command || card.description
      return <details key={card.key} className="setting-card configuration-card-details acp-agent-card" open={open}>
        <summary onClick={event => {
          event.preventDefault()
          if (open) closeEditor()
          else if (card.profile) edit(card.profile, card.index, card.key)
          else add(card.kind || 'custom', card.key)
        }}>
          <div className="configuration-card-header flex items-start gap-2.5">
            <span className="model-role-icon"><FluentIcon name={card.kind === 'custom' && !card.profile ? 'Edit' : 'Robot'} size={16}/></span>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2"><strong>{t(card.title)}</strong>{card.profile?.enabled ? <small className="acp-active-label">{t('ACTIVE')}</small> : null}</div>
              <div className="acp-agent-summary-detail">{t(detail)}</div>
            </div>
            <span className="text-[10px] font-[700] rounded-full px-2.5 py-1 shrink-0" data-tone={tone}>{t(status)}</span>
          </div>
        </summary>
        {open ? renderEditor() : null}
      </details>
    })}
    {locked && <p className="settings-card-description">{t(electronUnavailable ? 'Agent configuration is editable in the Electron app.' : 'Agent configuration is controlled by the parent process environment.')}</p>}
    {error && <p role="alert" className="settings-card-description text-red-700">{error}</p>}
  </div>
}
