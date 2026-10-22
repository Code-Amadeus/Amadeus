import { useEffect, useMemo, useRef, useState } from 'react'
import FluentIcon from './FluentIcon'
import { useI18n } from '../i18n'
import type { CharacterRecord } from './characterManagement'
import { foreignSessionCharacter, sessionCharacterNotice } from './chatSessionCharacter'

export interface ChatSessionContext {
  bindingKind: 'project' | 'work_item'
  projectId: string
  projectName: string
  defaultProjectId?: string
  defaultProjectName?: string
  workItemId?: string
  workItemTitle?: string
  canPromoteToProject?: boolean
}

export interface ChatSessionSummary {
  id: string
  title: string
  character_id?: string
  timestamp?: number
  message_count?: number
  context?: ChatSessionContext | null
}

export interface ChatProjectSummary {
  projectId: string
  name: string
  latestTaskTitle?: string
  counts?: {
    current?: number
    needsYou?: number
    running?: number
    history?: number
  }
}

interface Props {
  sessions: ChatSessionSummary[]
  projects: ChatProjectSummary[]
  activeId: string | null
  currentCharacterId: string
  characters: CharacterRecord[] | null
  connected: boolean
  startupCharacterLocked: boolean
  onNotice: (notice: string) => void
  artifactViewId: string
  onSelect: (id: string) => void
  onNew: () => void
  onNewProject: () => void
  onNewProjectSession: (projectId: string) => void
  onOpenProject: (projectId: string) => void
  onOpenDraftApps: () => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
}

type RailMode = 'chats' | 'artifacts'

interface SessionGroup {
  id: string
  projectId?: string
  label: string
  sessions: ChatSessionSummary[]
  latestAt: number
}

const SESSION_PAGE_SIZE = 30
const RAIL_CLOSE_DELAY_MS = 180

function sessionTimeValue(session: ChatSessionSummary): number {
  const value = Number(session.timestamp || 0)
  return Number.isFinite(value) ? value : 0
}

function sortSessions(sessions: ChatSessionSummary[], activeId: string | null): ChatSessionSummary[] {
  return [...sessions].sort((left, right) => {
    if (left.id === activeId && right.id !== activeId) return -1
    if (right.id === activeId && left.id !== activeId) return 1
    const byTime = sessionTimeValue(right) - sessionTimeValue(left)
    return byTime || right.id.localeCompare(left.id)
  })
}

function formatSessionTime(value: number | undefined): string {
  const raw = Number(value || 0)
  if (!Number.isFinite(raw) || raw <= 0) return ''
  const date = new Date(raw < 1_000_000_000_000 ? raw * 1000 : raw)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleDateString([], {
    month: 'short',
    day: 'numeric',
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  })
}

/** Inline title editor. Enter commits a changed, non-empty title; Escape or an unchanged title cancels. */
function SessionTitleInput({ title, onCommit, onCancel }: {
  title: string
  onCommit: (title: string) => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(title)
  // Enter or Escape unmounts the input, which can also report a blur; settle exactly once.
  const settled = useRef(false)
  const settle = (commit: boolean) => {
    if (settled.current) return
    settled.current = true
    const next = draft.trim()
    if (commit && next && next !== title) onCommit(next)
    else onCancel()
  }
  return (
    <input
      autoFocus
      className="chat-session-rename"
      value={draft}
      aria-label={t('Rename chat')}
      onChange={event => setDraft(event.target.value)}
      onFocus={event => event.currentTarget.select()}
      onKeyDown={event => {
        if (event.key === 'Enter') { event.preventDefault(); settle(true) }
        if (event.key === 'Escape') { event.preventDefault(); settle(false) }
      }}
      onBlur={() => settle(true)}
    />
  )
}

function SessionRow({
  session,
  active,
  editing,
  currentCharacterId,
  characters,
  connected,
  startupCharacterLocked,
  onNotice,
  onSelect,
  onStartRename,
  onRename,
  onCancelRename,
  onDelete,
}: {
  session: ChatSessionSummary
  active: boolean
  editing: boolean
  currentCharacterId: string
  characters: CharacterRecord[] | null
  connected: boolean
  startupCharacterLocked: boolean
  onNotice: (notice: string) => void
  onSelect: () => void
  onStartRename: () => void
  onRename: (title: string) => void
  onCancelRename: () => void
  onDelete: () => void
}) {
  const { t } = useI18n()
  const character = foreignSessionCharacter(session.character_id, currentCharacterId, characters)
  const canMutate = Boolean(connected && currentCharacterId && session.character_id === currentCharacterId)
  const mutationNotice = sessionCharacterNotice(
    session.character_id, currentCharacterId, characters, connected, startupCharacterLocked, t,
  )
  const characterLabel = character
    ? `${character.name}${character.available === false ? ` · ${t('Unavailable role')}` : ''}` : ''
  const characterTooltip = character
    ? `${t('Character')}: ${characterLabel}${character.name === character.id ? '' : ` (${character.id})`}` : ''
  const detail = session.context?.bindingKind === 'work_item'
    ? session.context.workItemTitle || t('Task')
    : session.message_count
      ? t('{count} messages', { count: session.message_count })
      : t('Draft')
  return (
    <div className="rail-session" data-active={active || undefined}>
      {editing ? (
        <SessionTitleInput title={session.title} onCommit={onRename} onCancel={onCancelRename} />
      ) : <button
        type="button"
        data-chat-session-id={session.id}
        onClick={() => { if (canMutate) onSelect(); else onNotice(mutationNotice) }}
        onDoubleClick={() => { if (canMutate) onStartRename(); else onNotice(mutationNotice) }}
        className="rail-session-button"
        title={canMutate ? session.title : `${session.title}\n${characterTooltip ? `${characterTooltip}\n` : ''}${mutationNotice}`}
      >
        <span className="rail-session-title">
          {session.title || t('Untitled chat')}
        </span>
        <span className="rail-session-meta">
          <span className="truncate">{detail}</span>
          {character && (
            <>
              <span aria-hidden="true">·</span>
              <span className="rail-session-character" title={characterTooltip}>
                {characterLabel}
              </span>
            </>
          )}
          <span aria-hidden="true">·</span>
          <span className="shrink-0">{formatSessionTime(session.timestamp)}</span>
        </span>
      </button>}
      <button
        type="button"
        disabled={!canMutate}
        onClick={event => { event.stopPropagation(); onDelete() }}
        title={mutationNotice || t('Delete chat')}
        aria-label={`${t('Delete')} ${session.title || t('chat')}`}
        className="icon-button rail-session-delete" data-size="small" data-tone="faint"
      >
        <FluentIcon name="Dismiss" size={14} />
      </button>
    </div>
  )
}

export default function ChatSessionRail({
  sessions,
  projects,
  activeId,
  currentCharacterId,
  characters,
  connected,
  startupCharacterLocked,
  onNotice,
  artifactViewId,
  onSelect,
  onNew,
  onNewProject,
  onNewProjectSession,
  onOpenProject,
  onOpenDraftApps,
  onRename,
  onDelete,
}: Props) {
  const { t } = useI18n()
  const [railHovered, setRailHovered] = useState(false)
  const [railFocused, setRailFocused] = useState(false)
  const [railMode, setRailMode] = useState<RailMode>(artifactViewId ? 'artifacts' : 'chats')
  const [previewGroupId, setPreviewGroupId] = useState('')
  const [query, setQuery] = useState('')
  const [visibleCounts, setVisibleCounts] = useState<Record<string, number>>({})
  // One chat title is editable at a time; the rail owns which one.
  const [editingId, setEditingId] = useState('')
  const railCloseTimerRef = useRef<number | null>(null)

  const { projectGroups, drafts } = useMemo(() => {
    const byProject = new Map<string, SessionGroup>()
    const draftSessions: ChatSessionSummary[] = []
    for (const project of projects) {
      byProject.set(project.projectId, {
        id: `project:${project.projectId}`,
        projectId: project.projectId,
        label: project.name || t('Untitled project'),
        sessions: [],
        latestAt: 0,
      })
    }
    for (const session of sessions) {
      const projectId = String(session.context?.projectId || '')
      if (!projectId) {
        draftSessions.push(session)
        continue
      }
      const current = byProject.get(projectId) || {
        id: `project:${projectId}`,
        projectId,
        label: session.context?.projectName || t('Untitled project'),
        sessions: [],
        latestAt: 0,
      }
      current.sessions.push(session)
      current.latestAt = Math.max(current.latestAt, sessionTimeValue(session))
      byProject.set(projectId, current)
    }
    const groups = [...byProject.values()]
      .map(group => ({ ...group, sessions: sortSessions(group.sessions, activeId) }))
      .sort((left, right) => right.latestAt - left.latestAt || left.label.localeCompare(right.label))
    return {
      projectGroups: groups,
      drafts: sortSessions(draftSessions, activeId),
    }
  }, [activeId, projects, sessions, t])

  const activeGroupId = useMemo(() => {
    const active = sessions.find(session => session.id === activeId)
    if (!active) return ''
    return active.context?.projectId ? `project:${active.context.projectId}` : 'drafts'
  }, [activeId, sessions])
  const railOpen = railHovered || railFocused

  const draftGroup = useMemo<SessionGroup>(() => ({
    id: 'drafts',
    label: t('Drafts'),
    sessions: drafts,
    latestAt: drafts.reduce((latest, session) => Math.max(latest, sessionTimeValue(session)), 0),
  }), [drafts, t])

  useEffect(() => {
    if (activeGroupId) setPreviewGroupId(activeGroupId)
  }, [activeGroupId])

  useEffect(() => {
    if (artifactViewId) setRailMode('artifacts')
  }, [artifactViewId])

  useEffect(() => () => {
    if (railCloseTimerRef.current !== null) window.clearTimeout(railCloseTimerRef.current)
  }, [])

  const cancelRailClose = () => {
    if (railCloseTimerRef.current !== null) {
      window.clearTimeout(railCloseTimerRef.current)
      railCloseTimerRef.current = null
    }
  }

  const openRail = () => {
    cancelRailClose()
    setRailHovered(true)
  }

  const scheduleRailClose = () => {
    cancelRailClose()
    railCloseTimerRef.current = window.setTimeout(() => {
      setRailHovered(false)
      railCloseTimerRef.current = null
    }, RAIL_CLOSE_DELAY_MS)
  }

  const normalizedQuery = query.trim().toLocaleLowerCase()
  const searchSessions = useMemo(() => {
    if (!normalizedQuery) return []
    return sortSessions(
      sessions.filter(session => [
        session.title,
        session.context?.projectName,
        session.context?.workItemTitle,
      ].some(value => String(value || '').toLocaleLowerCase().includes(normalizedQuery))),
      activeId,
    )
  }, [activeId, normalizedQuery, sessions])

  const previewGroup = useMemo<SessionGroup>(() => {
    if (normalizedQuery) {
      return {
        id: 'search',
        label: 'Search results',
        sessions: searchSessions,
        latestAt: searchSessions.reduce((latest, session) => Math.max(latest, sessionTimeValue(session)), 0),
      }
    }
    return projectGroups.find(group => group.id === previewGroupId)
      || (previewGroupId === 'drafts' ? draftGroup : undefined)
      || projectGroups.find(group => group.id === activeGroupId)
      || (activeGroupId === 'drafts' ? draftGroup : undefined)
      || projectGroups[0]
      || draftGroup
  }, [activeGroupId, draftGroup, normalizedQuery, previewGroupId, projectGroups, searchSessions])

  const visibleCount = visibleCounts[previewGroup.id] || SESSION_PAGE_SIZE
  const visibleSessions = previewGroup.sessions.slice(0, visibleCount)

  const renderProjectHeader = (group: SessionGroup) => {
    const selected = group.id === previewGroup.id && !normalizedQuery
    return (
      <div
        key={group.id}
        className="rail-group"
        data-selected={selected || undefined}
        onFocusCapture={() => setPreviewGroupId(group.id)}
      >
        <button
          type="button"
          onClick={() => setPreviewGroupId(group.id)}
          className="rail-group-button"
          title={`${t('Show')} ${group.label} ${t('chats')}`}
          aria-current={selected ? 'true' : undefined}
        >
          <FluentIcon name="ChevronRight" size={14} className="rail-faint" aria-hidden="true" />
          <span className="rail-group-label">{group.label}</span>
          <span className="rail-count">{group.sessions.length}</span>
        </button>
        {group.projectId && (
          <button
            type="button"
            onClick={() => onNewProjectSession(group.projectId || '')}
            title={`${t('New chat in')} ${group.label}`}
            aria-label={`${t('New chat in')} ${group.label}`}
            className="icon-button" data-size="small" data-tone="faint"
          >
            <FluentIcon name="Add" size={14} />
          </button>
        )}
      </div>
    )
  }

  return (
    <aside className="rail" aria-label={t('Chat history')}>
      <div
        className="rail-panel"
        data-open={railOpen || undefined}
        role="navigation"
        aria-label={t('Chat and artifact navigation')}
        aria-expanded={railOpen}
        tabIndex={0}
        onMouseEnter={openRail}
        onMouseLeave={scheduleRailClose}
        onFocusCapture={() => { cancelRailClose(); setRailFocused(true) }}
        onBlurCapture={event => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
            setRailFocused(false)
          }
        }}
      >
      {railOpen && <div className="rail-header">
        {(['chats', 'artifacts'] as RailMode[]).map(mode => (
          <button
            key={mode}
            type="button"
            onClick={() => setRailMode(mode)}
            aria-pressed={railMode === mode}
            className="rail-mode"
          >
            <FluentIcon name={mode === 'chats' ? 'Chat' : 'Tiles'} size={14} />
            <span className="truncate">{t(mode === 'chats' ? 'Chats' : 'Artifacts')}</span>
          </button>
        ))}
        {railMode === 'chats' && (
          <button
            type="button"
            onClick={onNew}
            title={t('New chat')}
            aria-label={t('New chat')}
            className="icon-button"
          >
            <FluentIcon name="Edit" size={16} />
          </button>
        )}
      </div>}

      {railOpen && railMode === 'chats' && (
        <div className="rail-body">
          <div className="rail-search-row">
            <input
              type="search"
              className="rail-search"
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder={t('Search chats')}
              aria-label={t('Search chats')}
            />
          </div>

          <div className="chat-scroll-area rail-projects">
            <div className="rail-section-row">
              <span className="rail-section-label">
                {t('PROJECTS')}
              </span>
              <button
                type="button"
                onClick={onNewProject}
                title={t('New Project')}
                aria-label={t('New Project')}
                className="icon-button" data-size="small" data-tone="faint"
              >
                <FluentIcon name="Add" size={14} />
              </button>
            </div>
            {projectGroups.length > 0
              ? projectGroups.map(renderProjectHeader)
              : <p className="rail-empty">{t('No Projects')}</p>}
            <hr className="rail-divider" />
            {renderProjectHeader(draftGroup)}
          </div>

          <section className="rail-sessions">
            <div className="rail-sessions-header">
              <span className="rail-sessions-title">
                {t(previewGroup.label)}
              </span>
              <span className="rail-count">{previewGroup.sessions.length}</span>
            </div>
            <div className="chat-scroll-area flex-1 min-h-0 overflow-y-auto">
              {visibleSessions.map(session => (
                <SessionRow
                  key={session.id}
                  session={session}
                  active={session.id === activeId}
                  editing={session.id === editingId}
                  currentCharacterId={currentCharacterId}
                  characters={characters}
                  connected={connected}
                  startupCharacterLocked={startupCharacterLocked}
                  onNotice={onNotice}
                  onSelect={() => onSelect(session.id)}
                  onStartRename={() => setEditingId(session.id)}
                  onRename={title => { setEditingId(''); onRename(session.id, title) }}
                  onCancelRename={() => setEditingId('')}
                  onDelete={() => onDelete(session.id)}
                />
              ))}
              {previewGroup.sessions.length === 0 && (
                <p className="rail-empty">
                  {t(normalizedQuery ? 'No matching chats' : 'No chats yet')}
                </p>
              )}
              {visibleCount < previewGroup.sessions.length && (
                <button
                  type="button"
                  onClick={() => setVisibleCounts(current => ({
                    ...current,
                    [previewGroup.id]: visibleCount + SESSION_PAGE_SIZE,
                  }))}
                  className="rail-more"
                >
                  {t('Show')} {Math.min(SESSION_PAGE_SIZE, previewGroup.sessions.length - visibleCount)} {t('more')}
                </button>
              )}
            </div>
          </section>
        </div>
      )}

      {railOpen && railMode === 'artifacts' && (
        <div className="rail-artifacts" aria-label={t('Artifact collections')}>
          <div className="rail-section-label rail-artifacts-label">
            {t('DRAFTS')}
          </div>
          <button
            type="button"
            onClick={onOpenDraftApps}
            aria-current={artifactViewId === 'drafts' ? 'page' : undefined}
            className="rail-link"
          >
            <FluentIcon name="Tiles" size={14} />
            <span className="flex-1 min-w-0">
              <span className="rail-link-title">{t('Draft artifacts')}</span>
              <span className="rail-link-meta">{t('Recent {count}', { count: 5 })}</span>
            </span>
          </button>

          <hr className="rail-divider" />
          <div className="rail-section-label rail-artifacts-label">
            {t('PROJECTS')}
          </div>
          <div className="chat-scroll-area flex-1 min-h-0 overflow-y-auto">
            {projectGroups.map(group => (
              <button
                key={`artifact:${group.id}`}
                type="button"
                onClick={() => onOpenProject(group.projectId || '')}
                aria-current={artifactViewId === group.projectId ? 'page' : undefined}
                className="rail-link"
              >
                <FluentIcon name="Work" size={14} />
                <span className="rail-link-title">{group.label}</span>
              </button>
            ))}
            {projectGroups.length === 0 && (
              <p className="rail-empty">{t('No Projects')}</p>
            )}
          </div>
        </div>
      )}
      </div>
    </aside>
  )
}
