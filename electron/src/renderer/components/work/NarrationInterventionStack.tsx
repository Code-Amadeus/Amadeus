import type { ProviderInspectionDetails, PermissionRequest, UserIntervention, WorkTurn } from './types'

interface Props {
  turn: WorkTurn
  details?: ProviderInspectionDetails
  connected: boolean
  onOpenActions: () => void
  onOpenAudit: () => void
  onPause: () => void
}

function PermissionCard({ permission, onOpenActions }: { permission: PermissionRequest; onOpenActions: () => void }) {
  return (
    <div className="crt-side-card permission">
      <span>Permission Required</span>
      <h3>{permission.action}</h3>
      <p><b>Provider</b> {permission.provider}</p>
      <p><b>Scope</b> {permission.scope}</p>
      {permission.reason && <p><b>Reason</b> {permission.reason}</p>}
      {permission.reversibility && <p><b>Reversible</b> {permission.reversibility}</p>}
      <div className="crt-side-actions">
        <button onClick={onOpenActions}>Review actions</button>
      </div>
    </div>
  )
}

function PendingInput({ input }: { input: UserIntervention }) {
  return (
    <div className={`crt-side-note ${input.status}`}>
      <b>{input.status}</b>
      <p>{input.summary}</p>
    </div>
  )
}

export default function NarrationInterventionStack({
  turn,
  details,
  connected,
  onOpenActions,
  onOpenAudit,
  onPause,
}: Props) {
  const primaryRisk = turn.risks[0]
  const narration = turn.status === 'blocked'
    ? 'This turn needs attention before it can continue.'
    : turn.status === 'review'
      ? 'The provider has produced a reviewable state. Inspect changes before accepting.'
      : turn.status === 'running'
        ? 'The provider is working. I will surface only meaningful changes and blockers.'
        : 'Ready to start or refine a work turn.'

  return (
    <aside className="crt-side-stack">
      <div className="crt-side-card voice">
        <span>Character / Voice State</span>
        <h3>{connected ? 'Narration layer online' : 'Backend offline'}</h3>
        <p>{narration}</p>
        <div className="crt-side-actions">
          <button onClick={onPause} disabled={turn.status !== 'running'}>Pause turn</button>
          <button onClick={onOpenAudit}>Audit</button>
        </div>
      </div>

      <div className="crt-side-card">
        <span>Narration</span>
        <p>{turn.summary || turn.intent}</p>
        {primaryRisk && <small>{primaryRisk.level}: {primaryRisk.summary}</small>}
      </div>

      <div className="crt-side-card">
        <span>Pending User Input</span>
        {turn.pendingInputs.length > 0
          ? turn.pendingInputs.map(input => <PendingInput key={input.id} input={input} />)
          : <p>No queued user intervention. New text or voice input will be attached to the current turn.</p>}
      </div>

      {turn.permissions.length > 0 ? (
        turn.permissions.map(permission => (
          <PermissionCard key={permission.id} permission={permission} onOpenActions={onOpenActions} />
        ))
      ) : (
        <div className="crt-side-card permission quiet">
          <span>Permissions</span>
          <p>{details?.busy || 'No permission lease is currently pending.'}</p>
        </div>
      )}
    </aside>
  )
}
