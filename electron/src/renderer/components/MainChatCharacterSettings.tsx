import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'
import { desktopCatalogFields } from '../../shared/configCatalog.js'
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
        <label className="settings-field-label" htmlFor="main-chat-character-override">{t(desktopCatalogFields.AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA.title['en-US'])}</label>
        <div className="settings-field-description">{t(locked
          ? 'Character prompt is controlled by your launch environment.'
          : 'Write a Japanese override for Kurisu, or leave blank and save to restore her built-in prompt shown as a placeholder.')}</div>
        <div className="settings-field-description">{t('VN, work commentary, English replies and Hybrid opening lines keep their own prompts. This setting does not change the active character, permissions, art or voice.')}</div>
        <textarea id="main-chat-character-override" rows={7} maxLength={desktopCatalogFields.AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA.max_length} value={draft}
          placeholder={defaultPrompt || t('Connect the backend to load Kurisu’s default Japanese persona.')}
          disabled={disabled} onChange={event => edit(event.target.value)} />
        {info.active === false ? <div className="settings-field-description">{t('This Kurisu setting is inactive for the current replies. It applies only when Kurisu is active and replies are Japanese.')}</div> : null}
        <div className="character-prompt-actions">
          <button type="button" disabled={disabled || !canSave || !dirty} onClick={() => void save()}>{t(saving ? 'Saving…' : 'Save character prompt')}</button>
          <button type="button" disabled={disabled} onClick={() => edit('')}>{t('Clear override')}</button>
        </div>
      </div>
    </CardShell>
  )
}
