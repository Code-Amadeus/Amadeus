import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import FluentIcon from './FluentIcon'
import { SettingsButton, SettingsField, StatusPill } from './SettingsPrimitives'
import { canLeaveSettingsEditors, DISCARD_CONNECTION_CHANGES, type SettingsEditorState } from './settingsDraft'

type Profile = {
  id: string; name: string; command: string; args: string[]; enabled: boolean; resume: boolean
  environment: Record<string, string>; config_options: Record<string, string>
}
type Option = { id: string; name: string; type: string; currentValue: string; options?: Array<{ value?: string; name: string; options?: Array<{ value: string; name: string }> }> }
export type AcpConfiguration = { provider_id: string; config_options?: Option[] }
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

export default function AcpProviders({ encoded, locked, electronUnavailable = false, restartPending = false, configurations, onSave, onRefresh, onEditorStateChange }: {
  encoded: string; locked: boolean; electronUnavailable?: boolean; restartPending?: boolean; configurations: AcpConfiguration[]
  onEditorStateChange?: (state: SettingsEditorState) => void
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
  const [removePending, setRemovePending] = useState(false)
  const initialDraft = useRef('')
  const dirty = draft !== null && JSON.stringify([draft, environment, options]) !== initialDraft.current
  useEffect(() => {
    onEditorStateChange?.({ dirty, busy })
    return () => onEditorStateChange?.({ dirty: false, busy: false })
  }, [dirty, busy, onEditorStateChange])
  const canLeave = () => canLeaveSettingsEditors([{ dirty, busy }], () => window.confirm(t(DISCARD_CONNECTION_CHANGES)))
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
    const next = { ...profile, args: profile.args || [], environment: profile.environment || {}, config_options: profile.config_options || {} }
    const nextEnvironment = Object.entries(next.environment).map(([k, v]) => `${k}=${v}`).join('\n')
    const nextOptions = Object.entries(next.config_options).map(([k, v]) => `${k}=${v}`).join('\n')
    initialDraft.current = JSON.stringify([next, nextEnvironment, nextOptions])
    setDraft(next)
    setEnvironment(nextEnvironment)
    setOptions(nextOptions)
    setRemovePending(false)
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

  const renderEditor = () => draft ? <div className="settings-form acp-agent-editor">
    <fieldset disabled={locked || busy} className="settings-form-body">
      <section className="settings-form-section" aria-label={t('Connection details')}>
        <h4 className="settings-card-title">{t('Connection details')}</h4>
        <SettingsField id="acp-name" label="Display name" description={selected >= 0 ? `${t('Agent id')}: ${draft.id}` : undefined}>
          <input id="acp-name" aria-describedby={selected >= 0 ? 'acp-name-hint' : undefined} className="settings-form-input" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })}/>
        </SettingsField>
        {selected < 0 ? <SettingsField id="acp-id" label="Agent id" description="Use a unique identifier for this connection.">
          <input id="acp-id" aria-describedby="acp-id-hint" className="settings-form-input settings-form-code" value={draft.id} onChange={e => setDraft({ ...draft, id: e.target.value })}/>
        </SettingsField> : null}
        <SettingsField id="acp-command" label="Executable" description="Choose an installed executable. On Windows, use node.exe for a JavaScript agent.">
          <input id="acp-command" aria-describedby="acp-command-hint" className="settings-form-input settings-form-code" value={draft.command} onChange={e => setDraft({ ...draft, command: e.target.value })}/>
        </SettingsField>
        <SettingsField id="acp-arguments" label="Arguments — one per line" description="For a JavaScript agent, put its entry file first. Commands run directly; no shell or automatic installation.">
          <textarea id="acp-arguments" aria-describedby="acp-arguments-hint" className="settings-form-input settings-form-code" rows={3} value={draft.args.join('\n')} onChange={e => setDraft({ ...draft, args: e.target.value.split('\n') })} placeholder={'C:\\path\\to\\installed-agent\\cli.js\n--profile\nacp'}/>
        </SettingsField>
        <SettingsField id="acp-enabled" label="Enable agent" description="Applies after saving and restarting the backend.">
          <div className="settings-form-switch"><input id="acp-enabled" aria-describedby="acp-enabled-hint" type="checkbox" role="switch" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })}/><span>{t(draft.enabled ? 'On' : 'Off')}</span></div>
        </SettingsField>
      </section>
      <section className="settings-form-section" aria-label={t('Credential references')}>
        <h4 className="settings-card-title">{t('Credential references')}</h4>
        <SettingsField id="acp-environment" label="Environment references" description="One child variable=Host variable per line. Use variable names, not secret values; leave empty for the agent’s existing login.">
          <textarea id="acp-environment" aria-describedby="acp-environment-hint" className="settings-form-input settings-form-code" rows={2} value={environment} onChange={e => setEnvironment(e.target.value)}/>
        </SettingsField>
      </section>
      <details className="settings-form-advanced">
        <summary><span>{t('Advanced agent settings')}</span><span className="settings-meta-text">{t('Model overrides and session reuse')}</span></summary>
        <div className="settings-form-advanced-body">
          <div className="settings-form-section-heading"><h4 className="settings-card-title">{t('Model and agent options')}</h4><SettingsButton onClick={() => void onRefresh().catch(e => setError(e instanceof Error ? e.message : String(e)))}>{t('Refresh available choices')}</SettingsButton></div>
          {known.length === 0 && <p className="settings-field-description">{t("Choices become available after this agent opens its first task. Leave overrides empty to use the agent's defaults.")}</p>}
          {known.filter(option => option.type === 'select').map((option, index) => {
            let selectedValue = ''
            try { selectedValue = pairs(options)[option.id] || '' } catch { /* preserve an unfinished advanced entry */ }
            const id = `acp-option-${index}`
            return <SettingsField id={id} label={option.name} key={option.id}><select id={id} className="settings-form-input" value={selectedValue} onChange={e => {
              try {
                const next = pairs(options)
                if (e.target.value) next[option.id] = e.target.value; else delete next[option.id]
                setOptions(Object.entries(next).map(([k, v]) => `${k}=${v}`).join('\n'))
              } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
            }}>
              <option value="">{t('Agent default')} ({option.currentValue})</option>
              {(option.options || []).flatMap(item => item.options || (item.value ? [{ value: item.value, name: item.name }] : [])).map(item => <option key={item.value} value={item.value}>{item.name}</option>)}
            </select></SettingsField>
          })}
          <SettingsField id="acp-options" label="Explicit option overrides" description="Optional. Use one name=value entry per line.">
            <textarea id="acp-options" aria-describedby="acp-options-hint" className="settings-form-input settings-form-code" value={options} rows={2} placeholder="model=agent-model-id" onChange={e => setOptions(e.target.value)}/>
          </SettingsField>
          <SettingsField id="acp-resume" label="Reuse persistent native sessions">
            <div className="settings-form-switch"><input id="acp-resume" type="checkbox" role="switch" checked={draft.resume} onChange={e => setDraft({ ...draft, resume: e.target.checked })}/><span>{t(draft.resume ? 'On' : 'Off')}</span></div>
          </SettingsField>
        </div>
      </details>
    </fieldset>
    <div className="settings-form-footer">
      {error ? <p role="alert" className="settings-form-error">{t(error)}</p> : null}
      {removePending ? <div className="settings-form-remove" role="group" aria-label={t('Confirm remove')}>
        <span>{t('Remove this saved connection?')}</span><SettingsButton disabled={busy} onClick={() => setRemovePending(false)}>{t('Keep connection')}</SettingsButton>
        <SettingsButton tone="danger" disabled={locked || busy} onClick={() => void save(profiles.filter((_, index) => index !== selected))}>{t('Confirm remove')}</SettingsButton>
      </div> : <>
        <div className="settings-form-save-hint" role="status">{t(dirty ? 'Unsaved changes' : 'Changes are saved together. Restart the backend to apply.')}</div>
        <div className="settings-form-actions">
          {selected >= 0 ? <SettingsButton tone="danger" disabled={locked || busy} onClick={() => setRemovePending(true)}>{t('Remove')}</SettingsButton> : <span />}
          <div className="settings-form-actions-end"><SettingsButton tone="quiet" disabled={busy} onClick={() => { if (canLeave()) closeEditor() }}>{t('Cancel')}</SettingsButton>
            <SettingsButton tone="primary" disabled={locked || busy || selected >= 0 && !dirty} onClick={() => {
              try {
                const updated = { ...draft, environment: pairs(environment), config_options: pairs(options) }
                void save(selected < 0 ? [...profiles, updated] : profiles.map((item, index) => index === selected ? updated : item))
              } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
            }}>{t(busy ? 'Saving…' : 'Save agent')}</SettingsButton></div>
        </div>
      </>}
    </div>
  </div> : null

  return <div className="settings-embedded-stack acp-settings-stack">
    <p className="settings-card-description">{t('Connect an installed agent. Save and restart the backend to apply changes. Each agent keeps its own model, tools and native permissions.')}</p>
    {cards.map(card => {
      const open = editorKey === card.key
      const status = card.profile ? (restartPending ? 'Restart required' : card.profile.enabled ? 'Enabled on startup' : 'Disabled') : card.kind === 'custom' ? 'Add' : 'Optional'
      const tone = card.profile && restartPending ? 'warning' : 'neutral'
      const model = card.profile?.config_options?.model
      const command = card.profile ? [card.profile.command, ...(card.profile.args || [])].filter(Boolean).join(' ') : ''
      const detail = model ? `${command} · ${model}` : command || card.description
      return <details key={card.key} className="setting-card configuration-card-details acp-agent-card" open={open}>
        <summary aria-disabled={busy} onClick={event => {
          event.preventDefault()
          if (!canLeave()) return
          if (open) closeEditor()
          else if (card.profile) edit(card.profile, card.index, card.key)
          else add(card.kind || 'custom', card.key)
        }}>
          <div className="configuration-card-header flex items-start gap-2.5">
            <span className="model-role-icon"><FluentIcon name={card.kind === 'custom' && !card.profile ? 'Edit' : 'Robot'} size={16}/></span>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2"><strong>{t(card.title)}</strong></div>
              <div className="acp-agent-summary-detail">{t(detail)}</div>
            </div>
            <StatusPill ok={false} tone={tone}>{t(status)}</StatusPill>
          </div>
        </summary>
        {open ? renderEditor() : null}
      </details>
    })}
    {locked && <p className="settings-card-description">{t(electronUnavailable ? 'Agent configuration is editable in the Electron app.' : 'Agent configuration is controlled by the parent process environment.')}</p>}
    {error && !draft && <p role="alert" className="settings-form-error">{t(error)}</p>}
  </div>
}
