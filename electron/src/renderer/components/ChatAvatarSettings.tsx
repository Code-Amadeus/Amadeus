import { useEffect, useState } from 'react'

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
        background: '#EEF2F6',
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
    <div className="setting-card" style={{ background: 'var(--surface)', border: '1px solid rgba(17,24,39,0.085)', borderRadius: 11, padding: '2px 14px', boxShadow: '0 1px 2px rgba(17,24,39,0.025)' }}>
      {rows.map((item, index) => {
        const src = avatars[item.role]
        return (
          <div key={item.role} className="flex items-center gap-3" style={{ minHeight: 66, borderTop: index ? '1px solid rgba(17,24,39,0.075)' : 'none' }}>
            <AvatarPreview src={src} fallback={item.fallback} />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-[600]" style={{ color: 'var(--text)', lineHeight: '18px' }}>{item.title}</div>
              <div className="text-[11px] mt-0.5" style={{ color: 'var(--muted)', lineHeight: '16px' }}>{item.detail}</div>
            </div>
            <button onClick={() => void choose(item.role)} disabled={Boolean(busy)} className="text-[10.5px] rounded-md px-2.5 disabled:opacity-40" style={{ height: 30, color: 'var(--text)', background: 'rgba(17,24,39,0.045)', border: 0 }}>{busy === item.role ? 'Opening…' : src ? 'Replace' : 'Choose image'}</button>
            {src ? <button onClick={() => void clear(item.role)} disabled={Boolean(busy)} title={`Restore default ${item.title}`} aria-label={`Restore default ${item.title}`} className="text-[18px] rounded-md disabled:opacity-35" style={{ width: 28, height: 28, color: 'var(--muted)', background: 'transparent', border: 0, lineHeight: 1 }}>×</button> : null}
          </div>
        )
      })}
      <div className="text-[10px] pb-2" style={{ color: 'var(--muted)', lineHeight: '15px' }}>A centered PNG copy is stored in the app's local assets. The original file is never modified.</div>
      {error ? <div className="text-[10.5px] pb-2" style={{ color: '#b42318' }}>{error}</div> : null}
    </div>
  )
}
