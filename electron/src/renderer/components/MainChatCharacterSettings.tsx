import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'
import { CardShell } from './SettingsPrimitives'

interface Props {
  savedOverride: string
  preview: unknown
  connected: boolean
  canSave: boolean
  locked: boolean
  saving: boolean
  onSave: (value: string) => Promise<boolean>
}

export default function MainChatCharacterSettings({ savedOverride, preview, connected, canSave, locked, saving, onSave }: Props) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(savedOverride)
  const [dirty, setDirty] = useState(false)
  const info = preview && typeof preview === 'object' ? preview as Record<string, unknown> : {}
  const defaultPrompt = typeof info.default === 'string' ? info.default : ''
  const effectivePrompt = connected && typeof info.effective === 'string' ? info.effective : ''
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
        <label className="settings-field-label" htmlFor="main-chat-character-preview">{t('Current Japanese character prompt')}</label>
        <div className="settings-field-description">{t(effectivePrompt
          ? info.active ? 'Active for Japanese Main Chat replies.' : 'Japanese setting saved; English replies use the built-in English character.'
          : 'Connect the backend to preview the current default or override.')}</div>
        <textarea id="main-chat-character-preview" readOnly rows={5} value={effectivePrompt} />
        <label className="settings-field-label" htmlFor="main-chat-character-override">{t('Japanese character override')}</label>
        <div className="settings-field-description">{t(locked
          ? 'Character prompt is controlled by your launch environment.'
          : 'Write the character prompt in Japanese. Leave blank and save to restore the built-in character. Applies to new replies; existing messages stay as they are.')}</div>
        <textarea id="main-chat-character-override" rows={7} maxLength={8192} value={draft}
          disabled={disabled} onChange={event => edit(event.target.value)} />
        <div className="character-prompt-actions">
          <button type="button" disabled={disabled || !canSave || !dirty} onClick={() => void save()}>{t(saving ? 'Saving…' : 'Save character prompt')}</button>
          <button type="button" disabled={disabled || !connected || !defaultPrompt} onClick={() => edit(defaultPrompt)}>{t('Use default as a starting point')}</button>
          <button type="button" disabled={disabled} onClick={() => edit('')}>{t('Clear override')}</button>
        </div>
      </div>
    </CardShell>
  )
}
