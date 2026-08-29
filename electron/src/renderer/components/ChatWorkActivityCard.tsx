import { useEffect, useMemo, useState } from 'react'
import type { ChatWorkActivityEntry, ChatWorkActivityRun } from './chatWorkActivity'

interface Props {
  activity: ChatWorkActivityRun
}

const STATUS_LABEL: Record<ChatWorkActivityRun['status'], string> = {
  running: 'Working',
  succeeded: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  stalled: 'Waiting',
}

const STATUS_COLOR: Record<ChatWorkActivityRun['status'], string> = {
  running: '#2563EB',
  succeeded: '#15803D',
  failed: '#C42B1C',
  cancelled: '#7C3AED',
  stalled: '#B45309',
}

function entryGlyph(entry: ChatWorkActivityEntry): string {
  if (entry.state === 'running') return '●'
  if (entry.state === 'succeeded') return '✓'
  if (entry.state === 'failed') return '×'
  if (entry.state === 'attention') return '!'
  return '·'
}

function durationLabel(activity: ChatWorkActivityRun, now: number): string {
  const end = activity.status === 'running' || activity.status === 'stalled'
    ? now
    : activity.updatedAt
  const seconds = Math.max(0, Math.round((end - activity.startedAt) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

export default function ChatWorkActivityCard({ activity }: Props) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (activity.status !== 'running' && activity.status !== 'stalled') return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [activity.status])

  const current = useMemo(() => (
    [...activity.entries].reverse().find(item => item.state === 'running')
      || activity.entries[activity.entries.length - 1]
  ), [activity.entries])
  const completed = activity.entries.filter(item => item.state === 'succeeded').length
  const failed = activity.entries.filter(item => item.state === 'failed').length
  const color = STATUS_COLOR[activity.status]

  return (
    <div
      className="flex items-start gap-[10px] py-[1px]"
      style={{ paddingInline: 'clamp(24px, 2.5vw, 34px)' }}
      data-work-activity-run={activity.runId}
    >
      <div style={{ width: 28, flexShrink: 0 }} />
      <details
        className="chat-work-activity flex-[7] min-w-0"
        style={{
          maxWidth: 'calc(100% - 56px)',
          color: 'var(--muted)',
        }}
      >
        <summary
          style={{
            cursor: 'pointer',
            listStyle: 'none',
            padding: '4px 2px',
            userSelect: 'none',
          }}
        >
          <div className="flex items-center gap-1.5" style={{ minWidth: 0 }}>
            <span
              className="chat-work-activity-chevron"
              aria-hidden="true"
              style={{ display: 'inline-block', color: 'var(--faint)', fontSize: 12, lineHeight: 1, transition: 'transform 120ms ease' }}
            >
              ›
            </span>
            <span
              aria-hidden="true"
              style={{
                width: 5,
                height: 5,
                borderRadius: 3,
                flexShrink: 0,
                background: color,
                opacity: activity.status === 'running' ? 0.9 : 0.72,
              }}
            />
            <b style={{ color: 'var(--muted)', fontSize: 10.5, fontWeight: 600, flexShrink: 0 }}>
              {activity.provider} · {STATUS_LABEL[activity.status]}
            </b>
            <span aria-hidden="true" style={{ color: 'var(--border-strong)', fontSize: 10 }}>·</span>
            <span className="truncate" style={{ color: 'var(--faint)', fontSize: 10.5, minWidth: 0 }}>
              {current?.title || activity.task || 'Preparing the provider run…'}
            </span>
            <span style={{ marginLeft: 'auto', color: 'var(--faint)', fontSize: 10.5, flexShrink: 0 }}>
              {durationLabel(activity, now)}
            </span>
          </div>
        </summary>

        <div style={{ margin: '2px 0 5px 8px', borderLeft: '1px solid var(--border)', padding: '2px 2px 4px 11px' }}>
          {activity.entries.length === 0 ? (
            <p style={{ color: 'var(--faint)', fontSize: 10.5, margin: '5px 0' }}>
              Waiting for observable provider activity.
            </p>
          ) : activity.entries.map(entry => (
            <div key={entry.id} style={{ display: 'grid', gridTemplateColumns: '12px minmax(0, 1fr)', gap: 4, padding: '3px 0' }}>
              <span
                aria-hidden="true"
                style={{
                  color: entry.state === 'failed'
                    ? '#C42B1C'
                    : entry.state === 'attention'
                      ? '#B45309'
                      : entry.state === 'succeeded'
                        ? '#15803D'
                        : 'var(--faint)',
                  fontSize: 10,
                  lineHeight: '15px',
                }}
              >
                {entryGlyph(entry)}
              </span>
              <div style={{ minWidth: 0 }}>
                <div style={{ color: 'var(--muted)', fontSize: 10.5, lineHeight: '15px', overflowWrap: 'anywhere' }}>
                  {entry.title}
                </div>
                {entry.detail && (
                  <pre
                    style={{
                      margin: '5px 0 0 0',
                      padding: '5px 7px',
                      maxHeight: 180,
                      overflow: 'auto',
                      borderRadius: 6,
                      background: 'var(--surface-alt)',
                      color: 'var(--muted)',
                      fontSize: 10,
                      lineHeight: 1.45,
                      whiteSpace: 'pre-wrap',
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {entry.detail}
                  </pre>
                )}
              </div>
            </div>
          ))}
          <div style={{ color: 'var(--faint)', fontSize: 9.5, margin: '4px 0 0 16px' }}>
            {completed} completed{failed ? ` · ${failed} failed` : ''} · observable activity only
          </div>
        </div>
        <style>{`.chat-work-activity[open] .chat-work-activity-chevron { transform: rotate(90deg); }`}</style>
      </details>
      <div className="flex-[3]" />
    </div>
  )
}
