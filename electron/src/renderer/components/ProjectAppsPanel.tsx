import { useState } from 'react'
import FluentIcon from './FluentIcon'
import type { ChatProjectSummary } from './ChatSessionRail'
import WorkFilesPanel, { type WorkFileSummary } from './WorkFilesPanel'

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
  files?: Omit<WorkFileSummary, 'projectId' | 'workItemId' | 'workTitle' | 'workState' | 'execution'>[]
  canPromote?: boolean
}

interface Props {
  project: ChatProjectSummary
  scope?: 'project' | 'drafts'
  apps: ProjectAppSummary[]
  files: WorkFileSummary[]
  readFile: (file: WorkFileSummary) => Promise<Record<string, unknown>>
  appsComplete: boolean
  filesComplete: boolean
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
  files,
  readFile,
  appsComplete,
  filesComplete,
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
  const [view, setView] = useState<'files' | 'apps'>('files')
  const complete = view === 'files' ? filesComplete : appsComplete
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
            {draftMode ? 'RECENT DRAFT ARTIFACTS' : 'PROJECT ARTIFACTS'}
          </span>
        </div>
        <button type="button" onClick={onRefresh} disabled={loading} title="Refresh artifacts" style={quietButton}>
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
                ? 'Files and interactive applications have separate recent lists. Application components stay with their application.'
                : 'Files and interactive applications kept by this Project. Promoted artifacts appear here without copying their files.'}
            </p>
          </div>

          <div role="tablist" aria-label="Artifact types" className="flex gap-2" style={{ marginBottom: 16 }}>
            <button type="button" role="tab" aria-selected={view === 'files'} onClick={() => setView('files')}
              style={{ ...quietButton, color: view === 'files' ? 'var(--accent)' : 'var(--muted)' }}>Files · {files.length}</button>
            <button type="button" role="tab" aria-selected={view === 'apps'} onClick={() => setView('apps')}
              style={{ ...quietButton, color: view === 'apps' ? 'var(--accent)' : 'var(--muted)' }}>Interactive apps · {apps.length}</button>
          </div>

          {feedback && (
            <p role="status" style={{ padding: '9px 11px', borderRadius: 8, border: '1px solid var(--border)', color: 'var(--muted)', background: 'var(--surface)', fontSize: 10 }}>
              {feedback}
            </p>
          )}

          {!complete && (
            <p role="status" style={{ color: 'var(--muted)', fontSize: 10 }}>
              {view === 'files' ? 'More files may exist outside this recent view.' : 'More applications may exist outside this recent view.'}
            </p>
          )}

          {!loading && (view === 'files' ? files.length === 0 : apps.length === 0) && (
            <div style={{ padding: '36px 22px', textAlign: 'center', border: '1px dashed var(--border-strong)', borderRadius: 12, background: 'var(--surface)' }}>
              <FluentIcon name="Tiles" size={24} style={{ color: 'var(--faint)', marginBottom: 8 }} />
              <strong className="block" style={{ color: 'var(--text)', fontSize: 12 }}>{view === 'files' ? 'No files yet' : 'No interactive apps ready to open'}</strong>
              <p style={{ margin: '6px auto 0', maxWidth: 420, color: 'var(--muted)', fontSize: 10, lineHeight: 1.5 }}>
                {view === 'files' ? 'Application components are available in their application details.' : 'Applications appear here when they are ready to open.'}
              </p>
            </div>
          )}

          {view === 'files' && files.length > 0 && <WorkFilesPanel files={files} read={readFile} />}
          {view === 'apps' && <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
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
                    <span>Execution: {app.execution || 'idle'}</span>
                    <span>{app.workState === 'review_ready' ? 'Awaiting review' : app.workState || 'open'}</span>
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
                  {Boolean(app.files?.length) && <details style={{ marginTop: 12 }}>
                    <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--muted)' }}>Application files · {app.files!.length}</summary>
                    <WorkFilesPanel files={app.files!.map(file => ({
                      projectId: app.projectId, workItemId: app.workItemId, workTitle: app.title,
                      workState: app.workState || 'open', execution: app.execution || 'idle', ...file,
                    }))} read={readFile} />
                  </details>}
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
          </div>}
        </div>
      </div>
    </section>
  )
}
