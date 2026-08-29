import type { ReactNode } from 'react'
import type { RichAction, RichTone } from './types'

interface Props {
  type: string
  title: string
  tone?: RichTone
  actions?: RichAction[]
  children: ReactNode
}

export default function RichCard({
  type,
  title,
  tone = 'neutral',
  actions = [],
  children,
}: Props) {
  return (
    <section className={`crt-rich-card ${tone}`}>
      <div className="crt-rich-card-head">
        <div>
          <span>{type}</span>
          <h3>{title}</h3>
        </div>
      </div>
      <div className="crt-rich-card-body">{children}</div>
      {actions.length > 0 && (
        <div className="crt-rich-actions">
          {actions.map(action => (
            <button key={action.label} onClick={action.onClick}>{action.label}</button>
          ))}
        </div>
      )}
    </section>
  )
}
