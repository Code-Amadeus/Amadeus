import { useEffect, useMemo, useState } from 'react'
import type { ChatWorkActivityEntry, ChatWorkActivityRun } from './chatWorkActivity'
import FluentIcon from './FluentIcon'
import { SettingsButton } from './SettingsPrimitives'
import { useI18n } from '../i18n'

interface Props {
  activity: ChatWorkActivityRun
  onPermissionDecision?: (
    activity: ChatWorkActivityRun,
    entry: ChatWorkActivityEntry,
    allow: boolean,
  ) => Promise<void>
}

const STATUS_LABEL: Record<ChatWorkActivityRun['status'], string> = {
  running: 'Working',
  succeeded: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  stalled: 'Waiting',
  orphaned: 'Outcome unknown',
}

// Status colors come from the theme (chat-activity CSS keyed by data-status / data-state).
function EntryMark({ entry }: { entry: ChatWorkActivityEntry }) {
  if (entry.state === 'succeeded') return <FluentIcon name="Checkmark" size={14} />
  if (entry.state === 'failed') return <FluentIcon name="Dismiss" size={14} />
  if (entry.state === 'running') return <span className="chat-activity-dot" />
  return <>{entry.state === 'attention' ? '!' : '·'}</>
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

export default function ChatWorkActivityCard({ activity, onPermissionDecision }: Props) {
  const { t } = useI18n()
  const [now, setNow] = useState(Date.now())
  const [permissionBusy, setPermissionBusy] = useState('')
  const [permissionError, setPermissionError] = useState({ requestId: '', message: '' })
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

  const decidePermission = async (entry: ChatWorkActivityEntry, allow: boolean) => {
    if (!onPermissionDecision || !entry.permission || permissionBusy) return
    setPermissionBusy(entry.permission.requestId)
    setPermissionError({ requestId: '', message: '' })
    try {
      await onPermissionDecision(activity, entry, allow)
    } catch (error) {
      setPermissionError({ requestId: entry.permission.requestId,
        message: error instanceof Error ? error.message : t('Permission decision failed') })
    } finally {
      setPermissionBusy('')
    }
  }

  return (
    <div className="chat-activity-row" data-work-activity-run={activity.runId}>
      <div className="chat-activity-gutter" />
      <details className="chat-work-activity" data-status={activity.status}>
        <summary className="chat-activity-summary">
          <FluentIcon name="ChevronRight" size={14} className="chat-work-activity-chevron" aria-hidden="true" />
          <span className="chat-activity-status-dot" aria-hidden="true" />
          <strong className="chat-activity-provider">
            {activity.provider} · {t(STATUS_LABEL[activity.status])}
          </strong>
          <span aria-hidden="true" className="chat-activity-separator">·</span>
          <span className="chat-activity-current">
            {current?.title || activity.task || t('Preparing the provider run…')}
          </span>
          <span className="chat-activity-duration">
            {durationLabel(activity, now)}
          </span>
        </summary>

        <div className="chat-activity-body">
          {activity.entries.length === 0 ? (
            <p className="chat-activity-empty">
              {t('Waiting for observable provider activity.')}
            </p>
          ) : activity.entries.map(entry => (
            <div key={entry.id} className="chat-activity-entry">
              <span aria-hidden="true" className="chat-activity-mark" data-state={entry.state}>
                <EntryMark entry={entry} />
              </span>
              <div className="min-w-0">
                <div className="chat-activity-entry-title">
                  {entry.title}
                </div>
                {entry.detail && (
                  <pre className="chat-activity-detail">
                    {entry.detail}
                  </pre>
                )}
                {entry.state === 'attention' && entry.permission && onPermissionDecision && (
                  <div
                    data-permission-request={entry.permission.requestId}
                    className="chat-activity-actions"
                  >
                    {entry.permission.options.includes('allow_once') && (
                      <SettingsButton
                        size="small"
                        disabled={Boolean(permissionBusy)}
                        onClick={() => { void decidePermission(entry, true) }}
                      >
                        {t('Allow once')}
                      </SettingsButton>
                    )}
                    {entry.permission.options.includes('deny') && (
                      <SettingsButton
                        size="small"
                        tone="quiet"
                        disabled={Boolean(permissionBusy)}
                        onClick={() => { void decidePermission(entry, false) }}
                      >
                        {t('Deny')}
                      </SettingsButton>
                    )}
                  </div>
                )}
                {permissionError.message && entry.permission?.requestId === permissionError.requestId && (
                  <div className="chat-activity-error">
                    {permissionError.message}
                  </div>
                )}
              </div>
            </div>
          ))}
          <div className="chat-activity-footer">
            {t('{count} completed', { count: completed })}{failed ? ` · ${t('{count} failed', { count: failed })}` : ''} · {t('observable activity only')}
          </div>
        </div>
      </details>
      <div className="chat-activity-spacer" />
    </div>
  )
}
