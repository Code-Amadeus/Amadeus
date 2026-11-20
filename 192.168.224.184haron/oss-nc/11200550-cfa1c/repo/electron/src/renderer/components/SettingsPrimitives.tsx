import { type ButtonHTMLAttributes, type ReactNode } from 'react'
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
  return <div className={`setting-card settings-row-card${vertical ? ' settings-row-card-vertical' : ''}`}>{children}</div>
}

export function CardIcon({ name }: { name: FluentIconName }) {
  return <span className="settings-card-icon" aria-hidden="true"><FluentIcon name={name} size={16} /></span>
}

export function StatusPill({ ok, tone, children }: { ok: boolean; tone?: 'success' | 'warning' | 'neutral'; children: ReactNode }) {
  return <span className="settings-status-pill" data-tone={tone || (ok ? 'success' : 'warning')}>{children}</span>
}

/**
 * A titled settings group. `action` (a SettingsButton) sits right of the title and description, like the
 * page header's restart button; a group that only links elsewhere has an action and no body.
 */
export function SettingsGroup({ title, detail, action, children }: { title: string; detail?: string; action?: ReactNode; children?: ReactNode }) {
  return (
    <section>
      {action ? <div className="settings-group-header"><GroupTitle detail={detail}>{title}</GroupTitle>{action}</div>
        : <GroupTitle detail={detail}>{title}</GroupTitle>}
      {children ? <div className="settings-group-body">{children}</div> : null}
    </section>
  )
}

/** Presentation only: each editor retains its own draft and save boundary. */
export function SettingsField({ id, label, description, children }: {
  id: string; label: string; description?: string; children: ReactNode
}) {
  const { t } = useI18n()
  return <div className="settings-form-field">
    <div className="settings-form-field-copy">
      <label className="settings-field-label" htmlFor={id}>{t(label)}</label>
      {description ? <div id={`${id}-hint`} className="settings-field-description">{t(description)}</div> : null}
    </div>
    <div className="settings-form-field-control">{children}</div>
  </div>
}

/** The one text button: four tones, and a small size for compact toolbars and inline actions. */
export function SettingsButton({ tone = 'secondary', size = 'regular', className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: 'primary' | 'secondary' | 'quiet' | 'danger'
  size?: 'regular' | 'small'
}) {
  return <button type="button" {...props} className={`settings-button ${className}`} data-tone={tone} data-size={size === 'small' ? 'small' : undefined} />
}
