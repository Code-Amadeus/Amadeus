import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'
import FluentIcon from './FluentIcon'
import { characterInitial, useActiveCharacter } from '../activeCharacter'

type ChatAvatarRole = 'user' | 'assistant'
type ChatAvatars = { user: string; assistant: string }

function AvatarPreview({ src, fallback }: { src: string; fallback: string }) {
  return <div className="avatar-preview">{src ? <img src={src} alt="" /> : fallback}</div>
}

export default function ChatAvatarSettings() {
  const { t } = useI18n()
  const activeCharacter = useActiveCharacter()
  const [avatars, setAvatars] = useState<ChatAvatars>({ user: '', assistant: '' })
  const [busy, setBusy] = useState<ChatAvatarRole | ''>('')
  const [error, setError] = useState('')

  useEffect(() => {
    void window.amadeus?.getChatAvatars().then(value => {
      if (value) setAvatars(value)
    }).catch(reason => setError(reason instanceof Error ? reason.message : 'Could not load chat avatars'))
  }, [])

  const choose = async (role: ChatAvatarRole) => {
    if (!window.amadeus) return
    setBusy(role)
    setError('')
    try {
      const result = await window.amadeus.selectChatAvatar(role)
      if (!result.ok) throw new Error(result.error || 'Could not save avatar')
      if (result.avatars) setAvatars(result.avatars)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not save avatar')
    } finally {
      setBusy('')
    }
  }

  const clear = async (role: ChatAvatarRole) => {
    if (!window.amadeus) return
    setBusy(role)
    setError('')
    try {
      const result = await window.amadeus.clearChatAvatar(role)
      if (!result.ok) throw new Error(result.error || 'Could not restore default avatar')
      if (result.avatars) setAvatars(result.avatars)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not restore default avatar')
    } finally {
      setBusy('')
    }
  }

  const rows: Array<{ role: ChatAvatarRole; title: string; detail: string; fallback: string }> = [
    { role: 'user', title: t('Your avatar'), detail: 'Shown beside your messages', fallback: 'U' },
    { role: 'assistant', title: activeCharacter
      ? t('{name} avatar', { name: activeCharacter.short_name }) : t('Assistant avatar'),
      detail: 'Shown beside Amadeus responses', fallback: characterInitial(activeCharacter) },
  ]

  return (
    <div className="setting-card settings-list-card">
      {rows.map(item => {
        const src = avatars[item.role]
        const uploadLabel = busy === item.role ? 'Opening…' : src ? 'Replace image' : 'Upload image'
        return (
          <div key={item.role} className="avatar-row">
            <AvatarPreview src={src} fallback={item.fallback} />
            <div className="flex-1 min-w-0">
              <div className="settings-card-title">{item.title}</div>
              <div className="settings-card-description">{t(item.detail)}</div>
            </div>
            <button
              onClick={() => void choose(item.role)}
              disabled={Boolean(busy)}
              title={`${t(uploadLabel)} · ${item.title}`}
              aria-label={`${t(uploadLabel)} · ${item.title}`}
              aria-busy={busy === item.role}
              className="avatar-upload-button flex items-center justify-center rounded-md disabled:opacity-40"
            >
              <FluentIcon name="PhotoUpload" size={16} className={busy === item.role ? 'animate-pulse' : undefined} />
            </button>
            {src ? <button onClick={() => void clear(item.role)} disabled={Boolean(busy)} title={`${t('Restore default')} ${item.title}`} aria-label={`${t('Restore default')} ${item.title}`} className="icon-button"><FluentIcon name="Dismiss" size={14} /></button> : null}
          </div>
        )
      })}
      <div className="settings-list-card-note">{t("A centered PNG copy is stored in the app's local assets. The original file is never modified.")}</div>
      {error ? <div className="settings-list-card-error">{error}</div> : null}
    </div>
  )
}
