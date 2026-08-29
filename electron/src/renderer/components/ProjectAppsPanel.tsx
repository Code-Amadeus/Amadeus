import FluentIcon from './FluentIcon'
import type { ChatProjectSummary } from './ChatSessionRail'

export interface ProjectAppSummary {
  projectId: string
  workItemId: string
  workTitle: string
  artifactId: string
  artifactRef: string
  appId: string
  title: string
  version: string
  objective?: string
  interactionSummary?: string
  modes: string[]
  revision: number
  updatedAt?: string
  workState?: string
  execution?: string
  artifactStatus?: string
  location?: string
  sourceSessionId?: string
  canPromote?: boolean
}

interface Props {
  project: ChatProjectSummary
  scope?: 'project' | 'drafts'
  apps: ProjectAppSummary[]
  complete: boolean
  loading: boolean
  actionKey: string
  feedback: string
  onBack: () => void
  onRefresh: () => void
  onNewChat: () => void
  onOpen: (app: ProjectAppSummary) => void
  onInteract: (app: ProjectAppSummary) => void
  onPromote?: (app: ProjectAppSummary) => void
}

function formatUpdatedAt(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

const quietButton: React.CSSProperties = {
  height: 30,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
  border: '1px solid var(--border)',
  borderRadius: 7,
  padding: '0 10px',
  background: 'var(--surface)',
  color: 'var(--muted)',
  cursor: 'pointer',
  fontSize: 10,
}

export default function ProjectAppsPanel({
  project,
  scope = 'project',
  apps,
  complete,
  loading,
  actionKey,
  feedback,
  onBack,
  onRefresh,
  onNewChat,
  onOpen,
  onInteract,
  onPromote,
}: Props) {
  const draftMode = scope === 'drafts'
  return (
    <section
      className="absolute inset-0 z-[4] flex flex-col"
      aria-label={`${project.name} artifacts`}
      style={{ background: 'var(--bg)' }}
    >
      <header
        className="flex items-center gap-2 shrink-0"
        style={{ minHeight: 48, padding: '7px 12px', borderBottom: '1px solid var(--border)', background: 'var(--surface)' }}
      >
        <button type="button" onClick={onBack} title="Back to chat" aria-label="Back to chat" style={{ ...quietButton, width: 30, padding: 0 }}>
          <FluentIcon name="LeftArrow" size={14} />
        </button>
        <div className="min-w-0 flex-1">
          <strong className="block truncate" style={{ color: 'var(--text)', fontSize: 12 }}>{project.name}</strong>
          <span style={{ color: 'var(--faint)', fontSize: 9 }}>
            {draftMode ? 'RECENT DRAFT ARTIFACTS' : 'PROJECT ARTIFACTS'} · {apps.length}
          </span>
        </div>
        <button type="button" onClick={onRefresh} disabled={loading} title="Refresh apps" style={quietButton}>
          <FluentIcon name="Sync" size={13} /> {loading ? 'Loading' : 'Refresh'}
        </button>
        <button type="button" onClick={onNewChat} style={{ ...quietButton, color: 'var(--accent)' }}>
          <FluentIcon name="Edit" size={13} /> New chat
        </button>
      </header>

      <div className="chat-scroll-area flex-1 overflow-y-auto" style={{ padding: 18 }}>
        <div style={{ maxWidth: 760, margin: '0 auto' }}>
          <div style={{ marginBottom: 16 }}>
            <h2 style={{ margin: 0, color: 'var(--text)', fontSize: 18, fontWeight: 650 }}>Artifacts</h2>
            <p style={{ margin: '5px 0 0', color: 'var(--muted)', fontSize: 11, lineHeight: 1.55 }}>
              {draftMode
                ? 'The five most recent verified Draft artifacts. Open one, continue its original conversation, or promote the Draft to keep it as a Project.'
                : 'Verified AUIP artifacts kept by this Project. Open the current revision, or enter its exact WorkItem conversation to continue with Amadeus.'}
            </p>
          </div>

          {feedback && (
            <p role="status" style={{ padding: '9px 11px', borderRadius: 8, border: '1px solid var(--border)', color: 'var(--muted)', background: 'var(--surface)', fontSize: 10 }}>
              {feedback}
            </p>
          )}

          {!complete && (
            <p role="status" style={{ color: 'var(--muted)', fontSize: 10 }}>
              {draftMode
                ? 'Older Drafts fall outside this bounded recent view.'
                : 'This Project has more WorkItems than this bounded view can show.'}
            </p>
          )}

          {!loading && apps.length === 0 && (
            <div style={{ padding: '36px 22px', textAlign: 'center', border: '1px dashed var(--border-strong)', borderRadius: 12, background: 'var(--surface)' }}>
              <FluentIcon name="Tiles" size={24} style={{ color: 'var(--faint)', marginBottom: 8 }} />
              <strong className="block" style={{ color: 'var(--text)', fontSize: 12 }}>No verified AUIP artifacts yet</strong>
              <p style={{ margin: '6px auto 0', maxWidth: 420, color: 'var(--muted)', fontSize: 10, lineHeight: 1.5 }}>
                {draftMode
                  ? 'No recent Draft contains a currently verified AUIP artifact.'
                  : 'Draft outputs stay with their WorkItem until you promote that Draft. Promoted artifacts appear here without copying their files.'}
              </p>
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
            {apps.map(app => {
              const opening = actionKey === `open:${app.workItemId}`
              const interacting = actionKey === `interact:${app.workItemId}`
              return (
                <article
                  key={app.workItemId}
                  style={{ padding: 14, border: '1px solid var(--border)', borderRadius: 11, background: 'var(--surface)', boxShadow: '0 5px 18px rgba(17,24,39,0.04)' }}
                >
                  <div className="flex items-start gap-3">
                    <span className="flex items-center justify-center shrink-0" style={{ width: 34, height: 34, borderRadius: 9, background: 'var(--surface-alt)', color: 'var(--accent)' }}>
                      <FluentIcon name="Tiles" size={17} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <strong className="block truncate" title={app.title} style={{ color: 'var(--text)', fontSize: 12 }}>{app.title}</strong>
                      <span className="block truncate" title={app.workTitle} style={{ marginTop: 2, color: 'var(--faint)', fontSize: 9 }}>
                        {app.version ? `v${app.version}` : 'version unknown'}{app.revision ? ` · revision ${app.revision}` : ''}
                      </span>
                    </div>
                  </div>

                  <p style={{ minHeight: 34, margin: '11px 0', color: 'var(--muted)', fontSize: 10, lineHeight: 1.5 }}>
                    {app.objective || app.interactionSummary || app.workTitle || 'Verified interactive application'}
                  </p>

                  <div className="flex items-center gap-2" style={{ marginBottom: 11, color: 'var(--faint)', fontSize: 9 }}>
                    <span>{app.artifactStatus || 'registered'}</span>
                    <span aria-hidden="true">·</span>
                    <span>{app.execution && app.execution !== 'idle' ? app.execution : app.workState || 'open'}</span>
                    {formatUpdatedAt(app.updatedAt) && <><span aria-hidden="true">·</span><span>{formatUpdatedAt(app.updatedAt)}</span></>}
                  </div>

                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={Boolean(actionKey)}
                      onClick={() => onOpen(app)}
                      style={{ ...quietButton, color: 'var(--text)' }}
                    >
                      <FluentIcon name="Play" size={13} /> {opening ? 'Opening' : 'Open'}
                    </button>
                    <button
                      type="button"
                      disabled={Boolean(actionKey)}
                      onClick={() => onInteract(app)}
                      style={{ ...quietButton, flex: 1, color: 'var(--accent)' }}
                    >
                      <FluentIcon name="Chat" size={13} /> {interacting ? 'Opening chat' : 'Interact with Amadeus'}
                    </button>
                  </div>
                  {draftMode && app.canPromote && onPromote && (
                    <button
                      type="button"
                      disabled={Boolean(actionKey)}
                      onClick={() => onPromote(app)}
                      style={{ ...quietButton, width: '100%', marginTop: 8, color: 'var(--accent)', borderStyle: 'dashed' }}
                    >
                      <FluentIcon name="Work" size={13} />
                      {actionKey === `promote:${app.workItemId}` ? 'Promoting' : 'Promote to Project'}
                    </button>
                  )}
                </article>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}
