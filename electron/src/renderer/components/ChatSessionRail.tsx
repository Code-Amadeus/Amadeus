import { useEffect, useMemo, useRef, useState } from 'react'
import FluentIcon from './FluentIcon'

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

function SessionRow({
  session,
  active,
  onSelect,
  onRename,
  onDelete,
}: {
  session: ChatSessionSummary
  active: boolean
  onSelect: () => void
  onRename: () => void
  onDelete: () => void
}) {
  const detail = session.context?.bindingKind === 'work_item'
    ? session.context.workItemTitle || 'Task'
    : session.message_count
      ? `${session.message_count} messages`
      : 'Draft'
  return (
    <div
      className="group flex items-center min-w-0"
      style={{
        margin: '1px 6px',
        borderRadius: 7,
        color: active ? 'var(--text)' : 'var(--muted)',
        backgroundColor: active ? 'var(--pressed)' : 'transparent',
      }}
      onMouseEnter={event => {
        if (!active) event.currentTarget.style.backgroundColor = 'var(--hover)'
      }}
      onMouseLeave={event => {
        if (!active) event.currentTarget.style.backgroundColor = 'transparent'
      }}
    >
      <button
        type="button"
        onClick={onSelect}
        onDoubleClick={onRename}
        className="flex-1 min-w-0 text-left border-none bg-transparent cursor-pointer"
        style={{ padding: '7px 5px 7px 9px', color: 'inherit' }}
        title={session.title}
      >
        <span className="block truncate" style={{ fontSize: 11, fontWeight: active ? 600 : 500 }}>
          {session.title || 'Untitled chat'}
        </span>
        <span className="flex items-center gap-1 min-w-0" style={{ marginTop: 2, color: 'var(--faint)', fontSize: 9 }}>
          <span className="truncate">{detail}</span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0">{formatSessionTime(session.timestamp)}</span>
        </span>
      </button>
      <button
        type="button"
        onClick={event => { event.stopPropagation(); onDelete() }}
        title="Delete chat"
        aria-label={`Delete ${session.title || 'chat'}`}
        className="opacity-0 group-hover:opacity-100 border-none bg-transparent cursor-pointer"
        style={{ color: 'var(--faint)', padding: '7px 7px 7px 3px', fontSize: 11 }}
      >
        x
      </button>
    </div>
  )
}

export default function ChatSessionRail({
  sessions,
  projects,
  activeId,
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
  const [railHovered, setRailHovered] = useState(false)
  const [railFocused, setRailFocused] = useState(false)
  const [railMode, setRailMode] = useState<RailMode>(artifactViewId ? 'artifacts' : 'chats')
  const [previewGroupId, setPreviewGroupId] = useState('')
  const [query, setQuery] = useState('')
  const [visibleCounts, setVisibleCounts] = useState<Record<string, number>>({})
  const railCloseTimerRef = useRef<number | null>(null)

  const { projectGroups, drafts } = useMemo(() => {
    const byProject = new Map<string, SessionGroup>()
    const draftSessions: ChatSessionSummary[] = []
    for (const project of projects) {
      byProject.set(project.projectId, {
        id: `project:${project.projectId}`,
        projectId: project.projectId,
        label: project.name || 'Untitled project',
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
        label: session.context?.projectName || 'Untitled project',
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
  }, [activeId, projects, sessions])

  const activeGroupId = useMemo(() => {
    const active = sessions.find(session => session.id === activeId)
    if (!active) return ''
    return active.context?.projectId ? `project:${active.context.projectId}` : 'drafts'
  }, [activeId, sessions])
  const railOpen = railHovered || railFocused

  const draftGroup = useMemo<SessionGroup>(() => ({
    id: 'drafts',
    label: 'Drafts',
    sessions: drafts,
    latestAt: drafts.reduce((latest, session) => Math.max(latest, sessionTimeValue(session)), 0),
  }), [drafts])

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
        className="flex items-center min-w-0"
        onMouseEnter={event => {
          if (!selected) event.currentTarget.style.background = 'var(--hover)'
        }}
        onMouseLeave={event => {
          if (!selected) event.currentTarget.style.background = 'transparent'
        }}
        onFocusCapture={() => setPreviewGroupId(group.id)}
        style={{ margin: '1px 5px', borderRadius: 7, background: selected ? 'var(--pressed)' : 'transparent' }}
      >
        <button
          type="button"
          onClick={() => setPreviewGroupId(group.id)}
          className="flex items-center flex-1 min-w-0 border-none bg-transparent cursor-pointer text-left"
          style={{ height: 29, padding: '0 4px 0 8px', color: selected ? 'var(--text)' : 'var(--muted)', fontSize: 10 }}
          title={`Show ${group.label} chats`}
          aria-current={selected ? 'true' : undefined}
        >
          <span aria-hidden="true" style={{ width: 13, color: 'var(--faint)' }}>›</span>
          <span className="flex-1 truncate" style={{ fontWeight: 600 }}>{group.label}</span>
          <span style={{ color: 'var(--faint)', fontSize: 9 }}>{group.sessions.length}</span>
        </button>
        {group.projectId && (
          <button
            type="button"
            onClick={() => onNewProjectSession(group.projectId || '')}
            title={`New chat in ${group.label}`}
            aria-label={`New chat in ${group.label}`}
            className="flex items-center justify-center border-none bg-transparent cursor-pointer"
            style={{ width: 23, height: 25, borderRadius: 6, color: 'var(--faint)', fontSize: 15 }}
          >
            +
          </button>
        )}
      </div>
    )
  }

  return (
    <aside
      className="shrink-0"
      aria-label="Chat history"
      style={{
        width: 44,
        position: 'relative',
        overflow: 'visible',
        zIndex: 8,
      }}
    >
      <div
        className="flex flex-col h-full"
        role="navigation"
        aria-label="Chat and artifact navigation"
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
        style={{
          position: 'absolute',
          inset: '0 auto 0 0',
          width: railOpen ? 206 : 44,
          borderRight: '1px solid var(--border)',
          backgroundColor: 'var(--surface-alt)',
          boxShadow: railOpen ? '8px 0 20px rgba(17,24,39,0.08)' : 'none',
          transition: 'width 140ms ease, box-shadow 140ms ease',
          overflow: 'hidden',
        }}
      >
      <div
        className="flex items-center shrink-0"
        style={{ minHeight: 41, padding: railOpen ? '5px 7px' : '6px 7px', gap: 3 }}
      >
        {railOpen ? (
          <>
            {(['chats', 'artifacts'] as RailMode[]).map(mode => (
              <button
                key={mode}
                type="button"
                onClick={() => setRailMode(mode)}
                aria-pressed={railMode === mode}
                className="flex items-center justify-center gap-1 border-none cursor-pointer"
                style={{
                  height: 30,
                  flex: 1,
                  minWidth: 0,
                  padding: '0 6px',
                  borderRadius: 7,
                  background: railMode === mode ? 'var(--pressed)' : 'transparent',
                  color: railMode === mode ? 'var(--text)' : 'var(--muted)',
                  fontSize: 10,
                  fontWeight: railMode === mode ? 650 : 500,
                }}
              >
                <FluentIcon name={mode === 'chats' ? 'Chat' : 'Tiles'} size={13} />
                <span className="truncate">{mode === 'chats' ? 'Chats' : 'Artifacts'}</span>
              </button>
            ))}
          </>
        ) : (
          <span className="flex items-center justify-center" style={{ width: 30, height: 30, color: 'var(--muted)' }}>
            <FluentIcon name={railMode === 'chats' ? 'Chat' : 'Tiles'} size={16} />
          </span>
        )}
        {railOpen && railMode === 'chats' && (
          <button
            type="button"
            onClick={onNew}
            title="New chat"
            aria-label="New chat"
            className="flex items-center justify-center border-none bg-transparent cursor-pointer"
            style={{ width: 30, height: 30, borderRadius: 6, color: 'var(--muted)' }}
          >
            <FluentIcon name="Edit" size={15} />
          </button>
        )}
      </div>

      {railOpen && railMode === 'chats' && (
        <div className="flex flex-col flex-1 min-h-0" style={{ paddingBottom: 7 }}>
          <div style={{ padding: '4px 8px 6px' }}>
            <input
              type="search"
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="Search chats"
              aria-label="Search chats"
              style={{
                width: '100%',
                height: 28,
                border: '1px solid var(--border)',
                borderRadius: 7,
                padding: '0 8px',
                outline: 'none',
                background: 'var(--surface)',
                color: 'var(--text)',
                fontSize: 10,
              }}
            />
          </div>

          <div className="chat-scroll-area shrink-0 overflow-y-auto" style={{ maxHeight: '38%', paddingBottom: 5 }}>
            <div className="flex items-center" style={{ padding: '1px 6px 2px 10px' }}>
              <span className="flex-1" style={{ color: 'var(--faint)', fontSize: 9, fontWeight: 700, letterSpacing: '0.08em' }}>
                PROJECTS
              </span>
              <button
                type="button"
                onClick={onNewProject}
                title="New Project"
                aria-label="New Project"
                className="flex items-center justify-center border-none bg-transparent cursor-pointer"
                style={{ width: 26, height: 26, borderRadius: 6, color: 'var(--faint)', fontSize: 16 }}
              >
                +
              </button>
            </div>
            {projectGroups.length > 0
              ? projectGroups.map(renderProjectHeader)
              : <p style={{ padding: '5px 10px 7px', color: 'var(--faint)', fontSize: 10 }}>No Projects</p>}
            <div style={{ height: 1, margin: '5px 9px', backgroundColor: 'var(--border)' }} />
            {renderProjectHeader(draftGroup)}
          </div>

          <section className="flex flex-col flex-1 min-h-0" style={{ borderTop: '1px solid var(--border)', paddingTop: 5 }}>
            <div className="flex items-center shrink-0" style={{ padding: '3px 10px 5px' }}>
              <span className="flex-1 truncate" style={{ color: 'var(--muted)', fontSize: 10, fontWeight: 650 }}>
                {previewGroup.label}
              </span>
              <span style={{ color: 'var(--faint)', fontSize: 9 }}>{previewGroup.sessions.length}</span>
            </div>
            <div className="chat-scroll-area flex-1 min-h-0 overflow-y-auto">
              {visibleSessions.map(session => (
                <SessionRow
                  key={session.id}
                  session={session}
                  active={session.id === activeId}
                  onSelect={() => onSelect(session.id)}
                  onRename={() => onRename(session.id, session.title)}
                  onDelete={() => onDelete(session.id)}
                />
              ))}
              {previewGroup.sessions.length === 0 && (
                <p style={{ padding: '9px 10px', color: 'var(--faint)', fontSize: 9 }}>
                  {normalizedQuery ? 'No matching chats' : 'No chats yet'}
                </p>
              )}
              {visibleCount < previewGroup.sessions.length && (
                <button
                  type="button"
                  onClick={() => setVisibleCounts(current => ({
                    ...current,
                    [previewGroup.id]: visibleCount + SESSION_PAGE_SIZE,
                  }))}
                  className="border-none bg-transparent cursor-pointer"
                  style={{ width: '100%', padding: '8px 10px', color: 'var(--accent)', fontSize: 9 }}
                >
                  Show {Math.min(SESSION_PAGE_SIZE, previewGroup.sessions.length - visibleCount)} more
                </button>
              )}
            </div>
          </section>
        </div>
      )}

      {railOpen && railMode === 'artifacts' && (
        <div className="flex flex-col flex-1 min-h-0" aria-label="Artifact collections" style={{ padding: '5px 6px 8px' }}>
          <div style={{ padding: '3px 5px 7px' }}>
            <div style={{ color: 'var(--faint)', fontSize: 9, fontWeight: 700, letterSpacing: '0.08em' }}>
              DRAFTS
            </div>
          </div>
          <button
            type="button"
            onClick={onOpenDraftApps}
            aria-current={artifactViewId === 'drafts' ? 'page' : undefined}
            className="flex items-center gap-2 min-w-0 border-none cursor-pointer text-left"
            style={{
              minHeight: 38,
              padding: '5px 8px',
              borderRadius: 7,
              background: artifactViewId === 'drafts' ? 'var(--pressed)' : 'transparent',
              color: artifactViewId === 'drafts' ? 'var(--text)' : 'var(--muted)',
            }}
          >
            <FluentIcon name="Tiles" size={14} />
            <span className="flex-1 min-w-0">
              <span className="block truncate" style={{ fontSize: 10, fontWeight: 650 }}>Draft artifacts</span>
              <span className="block" style={{ marginTop: 1, color: 'var(--faint)', fontSize: 9 }}>Recent 5</span>
            </span>
          </button>

          <div style={{ height: 1, margin: '8px 5px', backgroundColor: 'var(--border)' }} />
          <div style={{ padding: '1px 5px 6px', color: 'var(--faint)', fontSize: 9, fontWeight: 700, letterSpacing: '0.08em' }}>
            PROJECTS
          </div>
          <div className="chat-scroll-area flex-1 min-h-0 overflow-y-auto">
            {projectGroups.map(group => (
              <button
                key={`artifact:${group.id}`}
                type="button"
                onClick={() => onOpenProject(group.projectId || '')}
                aria-current={artifactViewId === group.projectId ? 'page' : undefined}
                className="flex items-center gap-2 min-w-0 border-none cursor-pointer text-left"
                style={{
                  width: '100%',
                  minHeight: 34,
                  margin: '1px 0',
                  padding: '5px 8px',
                  borderRadius: 7,
                  background: artifactViewId === group.projectId ? 'var(--pressed)' : 'transparent',
                  color: artifactViewId === group.projectId ? 'var(--text)' : 'var(--muted)',
                  fontSize: 10,
                  fontWeight: 600,
                }}
              >
                <FluentIcon name="Work" size={13} />
                <span className="flex-1 truncate">{group.label}</span>
              </button>
            ))}
            {projectGroups.length === 0 && (
              <p style={{ padding: '7px 8px', color: 'var(--faint)', fontSize: 9 }}>No Projects</p>
            )}
          </div>
        </div>
      )}
      </div>
    </aside>
  )
}
