import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'
import FluentIcon from './FluentIcon'

type ChatAvatarRole = 'user' | 'assistant'
type ChatAvatars = { user: string; assistant: string }

function AvatarPreview({ src, fallback }: { src: string; fallback: string }) {
  return (
    <div
      className="shrink-0 flex items-center justify-center overflow-hidden select-none"
      style={{
        width: 38,
        height: 38,
        borderRadius: 19,
        color: 'var(--accent)',
        background: 'var(--surface-alt)',
        border: '1px solid var(--border)',
        fontSize: 12,
        fontWeight: 650,
      }}
    >
      {src ? <img src={src} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : fallback}
    </div>
  )
}

export default function ChatAvatarSettings() {
  const { t } = useI18n()
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
    { role: 'user', title: 'Your avatar', detail: 'Shown beside your messages', fallback: 'U' },
    { role: 'assistant', title: 'Kurisu avatar', detail: 'Shown beside Amadeus responses', fallback: 'K' },
  ]

  return (
    <div className="setting-card" style={{ background: 'var(--surface)', border: '1px solid var(--card-border)', borderRadius: 11, padding: '4px 12px', boxShadow: '0 1px 2px color-mix(in srgb, var(--shadow-color) 25%, transparent)' }}>
      {rows.map((item, index) => {
        const src = avatars[item.role]
        const uploadLabel = busy === item.role ? 'Opening…' : src ? 'Replace image' : 'Upload image'
        return (
          <div key={item.role} className="flex items-center gap-3" style={{ minHeight: 66, borderTop: index ? '1px solid var(--divider)' : 'none' }}>
            <AvatarPreview src={src} fallback={item.fallback} />
            <div className="flex-1 min-w-0">
              <div className="settings-card-title">{t(item.title)}</div>
              <div className="settings-card-description">{t(item.detail)}</div>
            </div>
            <button
              onClick={() => void choose(item.role)}
              disabled={Boolean(busy)}
              title={`${t(uploadLabel)} · ${t(item.title)}`}
              aria-label={`${t(uploadLabel)} · ${t(item.title)}`}
              aria-busy={busy === item.role}
              className="avatar-upload-button flex items-center justify-center rounded-md disabled:opacity-40"
            >
              <FluentIcon name="PhotoUpload" size={18} className={busy === item.role ? 'animate-pulse' : undefined} />
            </button>
            {src ? <button onClick={() => void clear(item.role)} disabled={Boolean(busy)} title={`${t('Restore default')} ${t(item.title)}`} aria-label={`${t('Restore default')} ${t(item.title)}`} className="text-[18px] rounded-md disabled:opacity-35" style={{ width: 28, height: 28, color: 'var(--muted)', background: 'transparent', border: 0, lineHeight: 1 }}>×</button> : null}
          </div>
        )
      })}
      <div className="text-[10px] pb-2" style={{ color: 'var(--muted)', lineHeight: '15px' }}>{t("A centered PNG copy is stored in the app's local assets. The original file is never modified.")}</div>
      {error ? <div className="text-[10.5px] pb-2" style={{ color: 'var(--danger)' }}>{error}</div> : null}
    </div>
  )
}
