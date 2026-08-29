import { useEffect, useState } from 'react'
import RichCard from './RichCard'
import type { AuipExperienceProjection, ProviderInspectionDetails, OverlayMode, ProviderEvent, ProviderRun, WorkDockItem } from './types'
import { eventNarrative } from './workState'

interface Props {
  activeDetails?: ProviderInspectionDetails
  activeRun?: ProviderRun
  activeStatusLabel: string
  activeWorkItem?: WorkDockItem
  auipLaunchArtifactId: string
  auipLaunchFeedback?: { status: 'success' | 'error'; message: string }
  auipExperience?: AuipExperienceProjection
  canAcceptWork: boolean
  canArchiveWork: boolean
  cwd: string
  destinationLabel?: string
  destinationFeedback?: { status: string; message: string }
  detailEvents: ProviderEvent[]
  expanded: boolean
  onAccept: () => void
  onArchive: () => void
  onCollapse: () => void
  onLaunchAuip: (artifactId: string, mode?: string) => void
  onRetry: (amendmentText: string, authorizationPermissionRequestId?: string) => void
  phaseLabel: string
  provider: string
  refreshDiff: (attemptId?: string, runId?: string) => void
  refreshStatus: () => void
  retryBusy: boolean
  setOverlay: (overlay: OverlayMode) => void
  workItemDetail: Record<string, unknown> | null
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function rows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record).filter(item => Object.keys(item).length > 0) : []
}

function valueText(value: unknown, fallback = ''): string {
  return value === undefined || value === null || value === '' ? fallback : String(value)
}

function compact(value: unknown, limit = 280): string {
  return valueText(value).replace(/\s+/g, ' ').trim().slice(0, limit)
}

export default function WorkDetailDrawer({
  activeDetails,
  activeRun,
  activeStatusLabel,
  activeWorkItem,
  auipLaunchArtifactId,
  auipLaunchFeedback,
  auipExperience,
  canAcceptWork,
  canArchiveWork,
  cwd,
  destinationLabel,
  destinationFeedback,
  detailEvents,
  expanded,
  onAccept,
  onArchive,
  onCollapse,
  onLaunchAuip,
  onRetry,
  phaseLabel,
  provider,
  refreshDiff,
  refreshStatus,
  retryBusy,
  setOverlay,
  workItemDetail,
}: Props) {
  const [retryAmendment, setRetryAmendment] = useState('')

  useEffect(() => {
    setRetryAmendment('')
  }, [activeWorkItem?.id, activeWorkItem?.canRetry])

  if (!expanded) return null

  const detail = record(workItemDetail)
  const attempts = rows(detail.attempts)
  const artifacts = rows(detail.artifacts)
  const completions = rows(detail.completionHistory)
  const permissions = rows(detail.permissions)
  const auipApp = record(detail.auipApp)
  const auipArtifactId = valueText(auipApp.artifactId)
  const latestAttempt = attempts[attempts.length - 1] || {}
  const authorizationPermission = permissions.slice().reverse().find(permission => {
    const metadata = record(permission.metadata)
    return valueText(permission.attempt_id) === valueText(latestAttempt.attempt_id)
      && ['denied', 'expired'].includes(valueText(permission.status).toLowerCase())
      && metadata.kind === 'provider_permission'
      && metadata.diagnostic_only === true
      && metadata.retry_required === true
  })
  const authorizationPermissionId = valueText(
    authorizationPermission?.request_id || authorizationPermission?.id,
  )
  const workspace = activeWorkItem?.workspacePath || activeRun?.cwd || cwd || 'No workspace selected'
  const turnTitle = activeWorkItem?.title || activeRun?.task || 'No WorkItem selected'
  const goal = valueText(detail.goal, activeRun?.task || 'No persisted goal is available for this WorkItem.')
  const effectiveProvider = activeWorkItem?.provider || activeRun?.provider || provider
  const liveRows = detailEvents.map(eventNarrative)
  const workspaceActions = activeRun?.cwd
    ? [{ label: 'Git status', onClick: refreshStatus }]
    : []
  const lifecycleActions = [
    ...(canAcceptWork ? [{ label: 'Accept WorkItem', onClick: onAccept }] : []),
    ...(canArchiveWork ? [{ label: 'Archive WorkItem', onClick: onArchive }] : []),
    { label: 'Open audit', onClick: () => setOverlay('audit') },
  ]

  return (
    <div className="crt-detail-drawer">
      <div className="crt-detail-head">
        <div>
          <span>WorkItem Details</span>
          <small>{effectiveProvider} / {activeStatusLabel}</small>
        </div>
        <button onClick={onCollapse} title="Collapse WorkItem details" aria-label="Collapse WorkItem details">
          <span />
        </button>
      </div>
      <div className="crt-detail-scroll">
        <div className="crt-rich-transcript">
          <div className="crt-rich-turn-title">
            <span>{activeWorkItem ? 'persisted work item' : 'empty state'}</span>
            <h2>{turnTitle}</h2>
            <p>{goal}</p>
          </div>

          <RichCard
            type="workspace"
            title="Workspace Ownership"
            actions={[
              { label: 'Load diff', onClick: () => refreshDiff() },
              ...workspaceActions,
              { label: 'Provider trace', onClick: () => setOverlay('trace') },
            ]}
          >
            <div className="crt-rich-kv">
              <span>New work goes to</span>
              <b>{destinationLabel || 'a new draft'}</b>
              <span>WorkItem</span><b>{activeWorkItem?.id || 'Not created'}</b>
              <span>Viewing</span><b>{activeWorkItem ? activeWorkItem.title : 'No persisted task'}</b>
              <span>Workspace</span><b>{workspace}</b>
              <span>Git branch</span><b>{activeWorkItem?.branch || 'Not recorded'}</b>
              <span>Isolation</span><b>{activeWorkItem?.isolation || 'Not recorded'}</b>
              {activeWorkItem?.isScratch
                ? (
                  <>
                    <span>Scratch</span>
                    <b>
                      {activeWorkItem.canPromoteToProject
                        ? 'Draft — reachable in this conversation only'
                        : 'Draft — kept as a project'}
                    </b>
                  </>
                )
                : (
                  <>
                    <span>Project</span><b>{activeWorkItem?.projectName || 'Not recorded'}</b>
                  </>
                )}
              <span>Provider</span><b>{effectiveProvider}</b>
              <span>Execution</span><b>{activeWorkItem?.execution || activeStatusLabel}</b>
              <span>Completion</span><b>{activeWorkItem?.completion || 'Unknown'}</b>
              <span>Attention</span><b>{activeWorkItem?.attention || 'None'}</b>
              <span>Presentation phase</span><b>{phaseLabel}</b>
            </div>
            {destinationFeedback?.status === 'rejected'
              ? <p role="alert">{destinationFeedback.message}</p>
              : null}
            {activeWorkItem?.selectionReason && <p>{activeWorkItem.selectionReason}</p>}
          </RichCard>

          <RichCard type="live-context" title={`Run Attempts (${attempts.length})`}>
            {attempts.length > 0 ? (
              <table className="crt-rich-table compact">
                <thead><tr><th>#</th><th>Provider / status</th><th>Instruction / result</th><th>Review</th></tr></thead>
                <tbody>
                  {attempts.slice().reverse().slice(0, 20).map((attempt, index) => (
                    <tr key={valueText(attempt.attempt_id, `attempt-${index}`)}>
                      <td>{valueText(attempt.attempt_number, String(attempts.length - index))}</td>
                      <td>{valueText(attempt.provider, 'provider')} / {valueText(attempt.execution_status, 'unknown')}</td>
                      <td>{compact(attempt.task || attempt.result || attempt.error, 360) || 'No retained summary.'}</td>
                      <td>
                        <button
                          type="button"
                          onClick={() => refreshDiff(
                            valueText(attempt.attempt_id),
                            valueText(attempt.provider_run_id),
                          )}
                        >Diff</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p>No persisted provider attempts are attached to this WorkItem.</p>}
          </RichCard>

          {auipArtifactId && (
            <RichCard
              type="attached-experience"
              title={valueText(auipApp.title, 'AUIP application')}
              tone="info"
              actions={[
                {
                  label: auipLaunchArtifactId === auipArtifactId
                    ? 'Opening'
                    : 'Open',
                  onClick: () => onLaunchAuip(auipArtifactId, 'observe'),
                },
              ]}
            >
              <p>
                Verified AUIP {valueText(auipApp.version, 'v0')} capability. Opening starts a new
                experience; it does not amend or permanently change this WorkItem.
              </p>
              {auipLaunchFeedback && (
                <p role={auipLaunchFeedback.status === 'error' ? 'alert' : 'status'}>
                  {auipLaunchFeedback.message}
                </p>
              )}
            </RichCard>
          )}

          <RichCard
            type="file-list"
            title={`Business Artifacts (${artifacts.length})`}
            actions={[{ label: 'Load attributed diff', onClick: () => refreshDiff() }]}
          >
            {artifacts.length > 0 ? (
              <table className="crt-rich-table compact">
                <thead><tr><th>Kind</th><th>Reference</th><th>Status</th></tr></thead>
                <tbody>
                  {artifacts.slice(0, 40).map((artifact, index) => (
                    <tr key={valueText(artifact.artifact_id, `artifact-${index}`)}>
                      <td>{valueText(artifact.kind || artifact.role, 'artifact')}</td>
                      <td><code>{valueText(artifact.path || artifact.ref || artifact.identity_key, 'No path')}</code></td>
                      <td>
                        {valueText(artifact.status, 'registered')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p>No business artifact has been registered. Provider runtime metadata is not counted as a delivered file.</p>}
            {!auipArtifactId && auipLaunchFeedback && (
              <p role={auipLaunchFeedback.status === 'error' ? 'alert' : 'status'}>
                {auipLaunchFeedback.message}
              </p>
            )}
          </RichCard>

          {auipExperience && (
            <RichCard
              type="attached-experience"
              title={auipExperience.title}
              tone={auipExperience.status === 'completed'
                ? 'success'
                : auipExperience.status === 'disconnected'
                  ? 'warning'
                  : 'info'}
            >
              <div className="crt-rich-kv">
                <span>Connection</span>
                <b>{auipExperience.status === 'connecting'
                  ? 'Waiting for the application'
                  : auipExperience.status === 'active'
                    ? 'Connected'
                    : auipExperience.status === 'completed'
                      ? 'Experience complete'
                      : auipExperience.status === 'disconnected'
                        ? 'Connection lost'
                        : 'Closed'}</b>
                <span>Role</span><b>{auipExperience.stance || 'Not registered yet'}</b>
                <span>Participation</span><b>{auipExperience.engagementMode || 'observe'}</b>
                <span>Participant lane</span><b>{auipExperience.operatorStatus || 'idle'}</b>
                {auipExperience.latestAction && (
                  <><span>Latest accepted action</span><b>{auipExperience.latestAction}</b></>
                )}
                {auipExperience.latestEvent && (
                  <><span>Latest semantic event</span><b>{auipExperience.latestEvent}</b></>
                )}
                {auipExperience.terminal && (
                  <><span>Terminal fact</span><b>{auipExperience.terminal}</b></>
                )}
              </div>
              <p role={auipExperience.status === 'disconnected' ? 'alert' : 'status'}>
                {auipExperience.status === 'active'
                  ? 'The application is attached. Only host-accepted semantic facts appear here.'
                  : auipExperience.status === 'connecting'
                    ? 'The one-time Attach ticket has been issued; the application has not registered yet.'
                    : auipExperience.status === 'completed'
                      ? 'The terminal experience fact is retained without copying the application state into Slice.'
                      : auipExperience.status === 'disconnected'
                        ? 'The application disappeared. Amadeus will not claim that any pending action completed.'
                        : 'The application closed its AUIP session.'}
              </p>
            </RichCard>
          )}

          <RichCard type="verification" title={`Completion Assessments (${completions.length})`}>
            {completions.length > 0 ? (
              <table className="crt-rich-table compact">
                <thead><tr><th>Execution</th><th>Assessment / attention</th><th>Rationale</th></tr></thead>
                <tbody>
                  {completions.slice().reverse().slice(0, 12).map((completion, index) => (
                    <tr key={valueText(completion.assessment_id, `completion-${index}`)}>
                      <td>{valueText(completion.execution_status, 'unknown')}</td>
                      <td>{valueText(completion.completeness, 'unknown')} / {valueText(completion.attention, 'none')}</td>
                      <td>{compact(completion.rationale, 360) || 'No rationale retained.'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p>The provider may have ended, but no WorkItem completion assessment is available yet.</p>}
          </RichCard>

          <RichCard type="live-context" title={`Live Provider Events (${liveRows.length})`}>
            {liveRows.length > 0 ? (
              <div className="crt-rich-live">
                {liveRows.slice(-40).map((item, index) => (
                  <div key={`${item.label}-${index}`}>
                    <span>{item.label}</span>
                    <p>{item.text}</p>
                  </div>
                ))}
              </div>
            ) : <p>No live event window is retained in this process. Persisted attempts above remain authoritative after restart.</p>}
          </RichCard>

          {activeWorkItem?.canRetry && authorizationPermissionId && (
            <RichCard type="review-request" title="Denied Provider Operation" tone="attention">
              <div className="crt-rich-kv">
                <span>Capability</span><b>{valueText(authorizationPermission?.capability, 'provider tool')}</b>
                <span>Action</span><b>{valueText(authorizationPermission?.action, 'scoped action')}</b>
                <span>Scope</span><b>{rows(authorizationPermission?.scope).length
                  ? rows(authorizationPermission?.scope).map(entry => valueText(entry.path)).filter(Boolean).join(', ')
                  : (Array.isArray(authorizationPermission?.scope)
                    ? authorizationPermission.scope.map(value => valueText(value)).join(', ')
                    : valueText(authorizationPermission?.scope, 'provider-declared scope'))}</b>
                <span>Reason</span><b>{valueText(authorizationPermission?.reason, 'Provider policy denied the operation.')}</b>
              </div>
              <p>
                The Provider reported this after the tool had already failed, so the old run cannot resume.
                The action below creates one new attempt with a host-authenticated, per-request authorization block.
              </p>
              <div className="crt-permission-retry-action">
                <button
                  type="button"
                  onClick={() => onRetry('', authorizationPermissionId)}
                  disabled={retryBusy}
                >
                  {retryBusy ? 'Retrying' : 'Authorize this operation & Retry'}
                </button>
              </div>
            </RichCard>
          )}

          <RichCard type="review-request" title="WorkItem Disposition" tone="attention" actions={lifecycleActions}>
            <p>
              A provider run ending is not the same as accepting the WorkItem. Review its artifacts and assessment,
              then explicitly accept or archive it. Retry can replay the failed instruction unchanged or append one
              bounded correction; Resume only restores an interrupted run. A separate instruction starts a new WorkItem.
            </p>
            {activeWorkItem?.canRetry && (
              <div className="crt-retry-amendment">
                <label htmlFor="work-retry-amendment">Optional correction for this retry</label>
                <textarea
                  id="work-retry-amendment"
                  value={retryAmendment}
                  onChange={event => setRetryAmendment(event.target.value)}
                  maxLength={2000}
                  disabled={retryBusy}
                  placeholder="Leave empty to retry unchanged, or describe only what should be corrected."
                />
                <div>
                  <small>{retryAmendment.length} / 2000</small>
                  <button
                    type="button"
                    onClick={() => onRetry(retryAmendment)}
                    disabled={retryBusy}
                  >
                    {retryBusy ? 'Retrying' : retryAmendment.trim() ? 'Retry with correction' : 'Retry unchanged'}
                  </button>
                </div>
              </div>
            )}
            {compact(latestAttempt.error) && <p>Latest error: {compact(latestAttempt.error, 500)}</p>}
            {activeDetails?.busy && <p>{activeDetails.busy}</p>}
            {activeDetails?.error && <p>{activeDetails.error}</p>}
          </RichCard>
        </div>
      </div>
    </div>
  )
}
