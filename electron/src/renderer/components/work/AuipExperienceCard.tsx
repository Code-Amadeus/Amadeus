import { useState } from 'react'
import type { AuipExperienceProjection } from './types'

interface AuipExperienceCardProps {
  experience: AuipExperienceProjection
  onModeChange?: (mode: 'observe' | 'collaborate' | 'delegate') => void
  onStep?: () => void
  onLeave?: () => void
}

function connectionLabel(status: AuipExperienceProjection['status']) {
  if (status === 'active') return 'Connected'
  if (status === 'connecting') return 'Connecting'
  if (status === 'completed') return 'Experience complete'
  if (status === 'disconnected') return 'Connection lost'
  return 'Session closed'
}

function stanceLabel(stance?: string) {
  if (stance === 'participant') return 'Participating'
  if (stance === 'spectator') return 'Spectating'
  return 'Role pending'
}

function operatorLabel(status?: AuipExperienceProjection['operatorStatus']) {
  if (status === 'thinking') return 'Choosing an action'
  if (status === 'awaiting_receipt') return 'Waiting for app receipt'
  if (status === 'error') return 'Participant needs attention'
  return 'Participant idle'
}

export default function AuipExperienceCard({
  experience,
  onModeChange,
  onStep,
  onLeave,
}: AuipExperienceCardProps) {
  const [expanded, setExpanded] = useState(false)
  const summary = experience.latestNarration
    || experience.terminal
    || experience.latestAction
    || experience.latestEvent
    || 'Waiting for the next accepted experience fact'

  return (
    <aside
      className={`auip-experience-card ${expanded ? 'expanded' : ''} ${experience.status}`}
      aria-label={`${experience.title} attached experience`}
    >
      <button
        type="button"
        className="auip-experience-card-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded(value => !value)}
      >
        <span className="auip-experience-avatar" aria-hidden="true">K</span>
        <span className="auip-experience-heading">
          <small>ATTACHED EXPERIENCE</small>
          <strong>{experience.title}</strong>
          <span>{connectionLabel(experience.status)} · {stanceLabel(experience.stance)}</span>
        </span>
        <span className="auip-experience-disclosure" aria-hidden="true">{expanded ? '−' : '+'}</span>
      </button>
      {expanded && (
        <div className="auip-experience-card-body">
          <p>{summary}</p>
          {experience.status === 'active' && experience.appSessionId && (
            <div className="auip-experience-controls">
              <label>
                <span>Participation</span>
                <select
                  value={experience.engagementMode || 'observe'}
                  onChange={event => onModeChange?.(event.target.value as 'observe' | 'collaborate' | 'delegate')}
                >
                  <option value="observe">Watch</option>
                  <option value="collaborate">Take turns</option>
                  <option value="delegate">Let Kurisu play</option>
                </select>
              </label>
              <span className={`auip-operator-state ${experience.operatorStatus || 'idle'}`}>
                {operatorLabel(experience.operatorStatus)}
              </span>
              {experience.engagementMode === 'collaborate' && (
                <button
                  type="button"
                  onClick={onStep}
                  disabled={experience.operatorStatus === 'thinking' || experience.operatorStatus === 'awaiting_receipt'}
                >
                  Take one turn
                </button>
              )}
              <button type="button" className="quiet" onClick={onLeave}>Leave experience</button>
            </div>
          )}
          {experience.operatorError && <p role="alert">{experience.operatorError}</p>}
          <small>
            Host-accepted semantic facts only. The application's raw state stays outside the conversation.
          </small>
        </div>
      )}
    </aside>
  )
}
