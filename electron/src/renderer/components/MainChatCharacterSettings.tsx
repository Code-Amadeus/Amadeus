import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'
import { CardShell } from './SettingsPrimitives'

interface Props {
  savedOverride: string
  preview: unknown
  canSave: boolean
  locked: boolean
  saving: boolean
  onSave: (value: string) => Promise<boolean>
}

export default function MainChatCharacterSettings({ savedOverride, preview, canSave, locked, saving, onSave }: Props) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(savedOverride)
  const [dirty, setDirty] = useState(false)
  const info = preview && typeof preview === 'object' ? preview as Record<string, unknown> : {}
  const defaultPrompt = typeof info.default === 'string' ? info.default : ''
  const disabled = locked || saving

  useEffect(() => {
    if (!dirty) setDraft(savedOverride)
  }, [savedOverride, dirty])

  const edit = (value: string) => {
    setDraft(value)
    setDirty(true)
  }

  const save = async () => {
    if (await onSave(draft.trim())) setDirty(false)
  }

  return (
    <CardShell vertical>
      <div className="character-prompt-editor">
        <label className="settings-field-label" htmlFor="main-chat-character-override">{t('Japanese character override')}</label>
        <div className="settings-field-description">{t(locked
          ? 'Character prompt is controlled by your launch environment.'
          : 'Write a Japanese override, or leave blank and save to use the built-in character shown as a placeholder. Applies to new replies.')}</div>
        <textarea id="main-chat-character-override" rows={7} maxLength={8192} value={draft}
          placeholder={defaultPrompt || t('Connect the backend to load the default character prompt.')}
          disabled={disabled} onChange={event => edit(event.target.value)} />
        {info.active === false ? <div className="settings-field-description">{t('Japanese setting saved; English replies use the built-in English character.')}</div> : null}
        <div className="character-prompt-actions">
          <button type="button" disabled={disabled || !canSave || !dirty} onClick={() => void save()}>{t(saving ? 'Saving…' : 'Save character prompt')}</button>
          <button type="button" disabled={disabled} onClick={() => edit('')}>{t('Clear override')}</button>
        </div>
      </div>
    </CardShell>
  )
}
