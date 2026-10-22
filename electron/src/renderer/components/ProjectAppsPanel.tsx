import { useState } from 'react'
import FluentIcon from './FluentIcon'
import type { ChatProjectSummary } from './ChatSessionRail'
import WorkFilesPanel, { type WorkFileSummary } from './WorkFilesPanel'
import { SettingsButton } from './SettingsPrimitives'
import { useI18n } from '../i18n'

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
  const { t } = useI18n()
  const draftMode = scope === 'drafts'
  const [view, setView] = useState<'files' | 'apps'>('files')
  const complete = view === 'files' ? filesComplete : appsComplete
  return (
    <section className="artifacts-panel" aria-label={t('{name} artifacts', { name: project.name })}>
      <header className="artifacts-header">
        <button type="button" onClick={onBack} title={t('Back to chat')} aria-label={t('Back to chat')} className="icon-button">
          <FluentIcon name="LeftArrow" size={14} />
        </button>
        <div className="min-w-0 flex-1">
          <strong className="artifacts-title">{project.name}</strong>
          <span className="artifacts-kicker">
            {t(draftMode ? 'RECENT DRAFT ARTIFACTS' : 'PROJECT ARTIFACTS')}
          </span>
        </div>
        <SettingsButton size="small" onClick={onRefresh} disabled={loading} title={t('Refresh artifacts')}>
          <FluentIcon name="Sync" size={14} /> {t(loading ? 'Loading' : 'Refresh')}
        </SettingsButton>
        <SettingsButton size="small" onClick={onNewChat}>
          <FluentIcon name="Edit" size={14} /> {t('New chat')}
        </SettingsButton>
      </header>

      <div className="chat-scroll-area artifacts-scroll">
        <div className="artifacts-content">
          <div className="artifacts-intro">
            <h2 className="artifacts-heading">{t('Artifacts')}</h2>
            <p className="artifacts-description">
              {t(draftMode
                ? 'Files and interactive applications have separate recent lists. Application components stay with their application.'
                : 'Files and interactive applications kept by this Project. Promoted artifacts appear here without copying their files.')}
            </p>
          </div>

          <div role="tablist" aria-label={t('Artifact types')} className="capability-page-tabs artifacts-tabs">
            <button type="button" role="tab" aria-selected={view === 'files'} onClick={() => setView('files')}>{t('Files')} · {files.length}</button>
            <button type="button" role="tab" aria-selected={view === 'apps'} onClick={() => setView('apps')}>{t('Interactive apps')} · {apps.length}</button>
          </div>

          {feedback && (
            <p role="status" className="artifacts-feedback">
              {feedback}
            </p>
          )}

          {!complete && (
            <p role="status" className="artifacts-note">
              {t(view === 'files' ? 'More files may exist outside this recent view.' : 'More applications may exist outside this recent view.')}
            </p>
          )}

          {!loading && (view === 'files' ? files.length === 0 : apps.length === 0) && (
            <div className="artifacts-empty">
              <FluentIcon name="Tiles" size={24} className="artifacts-empty-icon" />
              <strong className="artifacts-empty-title">{t(view === 'files' ? 'No files yet' : 'No interactive apps ready to open')}</strong>
              <p className="artifacts-empty-text">
                {t(view === 'files' ? 'Application components are available in their application details.' : 'Applications appear here when they are ready to open.')}
              </p>
            </div>
          )}

          {view === 'files' && files.length > 0 && <WorkFilesPanel files={files} read={readFile} />}
          {view === 'apps' && <div className="artifacts-grid">
            {apps.map(app => {
              const opening = actionKey === `open:${app.workItemId}`
              const interacting = actionKey === `interact:${app.workItemId}`
              return (
                <article key={app.workItemId} className="artifacts-card">
                  <div className="flex items-start gap-3">
                    <span className="artifacts-card-icon">
                      <FluentIcon name="Tiles" size={16} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <strong className="artifacts-card-title" title={app.title}>{app.title}</strong>
                      <span className="artifacts-card-meta" title={app.workTitle}>
                        {app.version ? `v${app.version}` : t('version unknown')}{app.revision ? ` · ${t('revision {count}', { count: app.revision })}` : ''}
                      </span>
                    </div>
                  </div>

                  <p className="artifacts-card-text">
                    {app.objective || app.interactionSummary || app.workTitle || t('Verified interactive application')}
                  </p>

                  <div className="artifacts-card-facts">
                    <span>{app.artifactStatus || 'registered'}</span>
                    <span aria-hidden="true">·</span>
                    <span>{t('Execution')}: {app.execution || 'idle'}</span>
                    <span>{app.workState === 'review_ready' ? t('Awaiting review') : app.workState || 'open'}</span>
                    {formatUpdatedAt(app.updatedAt) && <><span aria-hidden="true">·</span><span>{formatUpdatedAt(app.updatedAt)}</span></>}
                  </div>

                  <div className="flex items-center gap-2">
                    <SettingsButton size="small" disabled={Boolean(actionKey)} onClick={() => onOpen(app)}>
                      <FluentIcon name="Play" size={14} /> {t(opening ? 'Opening' : 'Open')}
                    </SettingsButton>
                    <SettingsButton size="small" tone="primary" className="flex-1" disabled={Boolean(actionKey)} onClick={() => onInteract(app)}>
                      <FluentIcon name="Chat" size={14} /> {t(interacting ? 'Opening chat' : 'Interact with Amadeus')}
                    </SettingsButton>
                  </div>
                  {Boolean(app.files?.length) && <details className="artifacts-card-files">
                    <summary>{t('Application files')} · {app.files!.length}</summary>
                    <WorkFilesPanel files={app.files!.map(file => ({
                      projectId: app.projectId, workItemId: app.workItemId, workTitle: app.title,
                      workState: app.workState || 'open', execution: app.execution || 'idle', ...file,
                    }))} read={readFile} />
                  </details>}
                  {draftMode && app.canPromote && onPromote && (
                    <SettingsButton size="small" className="w-full mt-2" disabled={Boolean(actionKey)} onClick={() => onPromote(app)}>
                      <FluentIcon name="Work" size={14} />
                      {t(actionKey === `promote:${app.workItemId}` ? 'Promoting' : 'Promote to Project')}
                    </SettingsButton>
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
