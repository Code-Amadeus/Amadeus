import { type ReactNode } from 'react'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import { useI18n } from '../i18n'

export function GroupTitle({ children, detail }: { children: string; detail?: string }) {
  const { t } = useI18n()
  return (
    <div>
      <h3 className="settings-group-title">{t(children)}</h3>
      {detail ? <div className="settings-group-description">{t(detail)}</div> : null}
    </div>
  )
}

export function CardShell({ children, vertical = false }: { children: ReactNode; vertical?: boolean }) {
  return (
    <div
      className={`setting-card flex ${vertical ? 'flex-col items-stretch' : 'items-center'}`}
      style={{
        minHeight: vertical ? undefined : 64,
        backgroundColor: 'var(--surface)',
        border: '1px solid var(--card-border)',
        borderRadius: 11,
        padding: 12,
        boxShadow: '0 1px 2px color-mix(in srgb, var(--shadow-color) 25%, transparent)',
      }}
    >
      {children}
    </div>
  )
}

export function CardIcon({ name }: { name: FluentIconName }) {
  return (
    <>
      <div className="shrink-0 flex items-center justify-center" style={{ width: 24, color: 'var(--muted)' }}>
        <FluentIcon name={name} size={17} />
      </div>
      <div style={{ width: 11, flexShrink: 0 }} />
    </>
  )
}

export function StatusPill({ ok, tone, children }: { ok: boolean; tone?: 'success' | 'warning' | 'neutral'; children: ReactNode }) {
  const resolvedTone = tone || (ok ? 'success' : 'warning')
  return (
    <span
      className="settings-status-pill text-[10px] font-[700] rounded-full px-2.5 py-1 shrink-0"
      data-tone={resolvedTone}
    >
      {children}
    </span>
  )
}

export function SettingsGroup({ title, detail, children }: { title: string; detail?: string; children: ReactNode }) {
  return (
    <section>
      <GroupTitle detail={detail}>{title}</GroupTitle>
      <div className="settings-group-body">{children}</div>
    </section>
  )
}
