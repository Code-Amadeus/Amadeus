import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'

type FrameSource = { mode: string; budgetMiB: number; reason: string }
type Snapshot = { surfaces?: Record<string, { state?: string; diagnostic?: { frame_source?: FrameSource } }> }
interface Props {
  connected: boolean
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
}

export default function GraphicsFrameSourceStatus({ connected, send, subscribe }: Props) {
  const { t } = useI18n()
  const [snapshot, setSnapshot] = useState<Snapshot>({})
  useEffect(() => {
    setSnapshot({})
    if (!connected) return
    let active = true
    const apply = (value: Record<string, unknown>) => { if (active) setSnapshot(value as Snapshot) }
    const unsubscribe = subscribe('visual.updated', apply)
    send('visual.get', {}).then(apply).catch(() => {})
    return () => { active = false; unsubscribe() }
  }, [connected, send, subscribe])
  const sources = Object.entries(snapshot.surfaces || {}).filter(([, state]) =>
    state.state !== 'unloaded' && state.diagnostic?.frame_source)
  const reasons: Record<string, string> = {
    static_selected: 'Static frames selected.',
    video_pack_missing: 'No video package installed; using the existing static path.',
    video_pack_invalid: 'Video package does not match; using the existing static path.',
    checking_video: 'Checking video support; static playback remains active.',
    video_unavailable: 'Video is unavailable on this surface; using the static path.',
  }
  return <div className="settings-meta-text" role="status">
    <strong>{t('Actual animation frame source')}</strong>
    {sources.length ? sources.map(([surface, status]) => {
      const source = status.diagnostic!.frame_source!
      return <div key={surface}>
        {t(surface === 'wallpaper' ? 'Wallpaper' : 'Character window')}: {' '}
        {t(source.mode === 'video' ? 'Video with static heads' : 'Static frames')} · {source.budgetMiB} MiB
        {reasons[source.reason] ? <div>{t(reasons[source.reason])}</div> : null}
      </div>
    }) : <div>{t('Waiting for a renderer to report its active source.')}</div>}
  </div>
}
