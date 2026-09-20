import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import FluentIcon from './FluentIcon'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  connected: boolean
}

type Tab = 'overview' | 'memories' | 'schedule'

type MemoryItem = {
  id: string
  memory_key: string
  kind: string
  summary: string
  pinned: boolean
  retention_tier: string
  retention_score: number
  priority_class: string
  updated_at: number
}

type ScheduleItem = {
  item_id: string
  ordinal: number
  category: string
  title: string
  starts_at: number
  ends_at: number
  status: string
  thread_key: string
}

function asRecord(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, any>
    : {}
}

function fmtTime(value: unknown): string {
  const seconds = Number(value || 0)
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  return new Date(seconds * 1000).toLocaleString()
}

function Metric({ label, value, detail }: { label: string; value: string | number; detail?: string }) {
  return (
    <div className="rounded-xl border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <div className="text-[11px] font-[650] uppercase tracking-[0.08em]" style={{ color: 'var(--muted)' }}>{label}</div>
      <div className="text-[24px] font-[700] mt-1" style={{ color: 'var(--text)' }}>{value}</div>
      {detail ? <div className="text-[11.5px] mt-1" style={{ color: 'var(--muted)' }}>{detail}</div> : null}
    </div>
  )
}

function ActionButton({ children, onClick, disabled, danger = false }: {
  children: ReactNode
  onClick: () => void
  disabled?: boolean
  danger?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg px-3 py-2 text-[12px] font-[650] disabled:opacity-40"
      style={{
        background: danger ? '#FDE7E9' : 'var(--surface)',
        color: danger ? '#A4262C' : 'var(--text)',
        border: '1px solid var(--border)',
      }}
    >{children}</button>
  )
}

export default function ContinuityPage({ send, connected }: Props) {
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<Record<string, any>>({})
  const [memories, setMemories] = useState<MemoryItem[]>([])
  const [schedule, setSchedule] = useState<Record<string, any>>({})
  const [sessionScope, setSessionScope] = useState('')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('')
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(async () => {
    if (!connected) return
    setBusy('refresh')
    try {
      // Memory and relationship views are Session-scoped: they follow the
      // active chat dialogue, exactly like Main Chat does.
      const sessionResult = await send('session.list', {})
      const scope = String(sessionResult.current_session_id || '')
      setSessionScope(scope)
      const [statusResult, memoryResult, scheduleResult] = await Promise.all([
        send('continuity.status', scope ? { scope } : {}),
        send('continuity.memory.list', scope ? { limit: 500, scope } : { limit: 500 }),
        send('continuity.life.schedule', {}),
      ])
      setStatus(asRecord(statusResult))
      setMemories(Array.isArray(memoryResult.memories) ? memoryResult.memories as MemoryItem[] : [])
      setSchedule(asRecord(scheduleResult))
    } catch (error) {
      setFeedback({ ok: false, text: error instanceof Error ? error.message : String(error) })
    } finally {
      setBusy('')
    }
  }, [connected, send])

  useEffect(() => { void load() }, [load])

  const act = useCallback(async (
    name: string,
    fn: () => Promise<Record<string, unknown>>,
    message: string,
  ) => {
    setBusy(name)
    try {
      await fn()
      setFeedback({ ok: true, text: message })
      await load()
    } catch (error) {
      setFeedback({ ok: false, text: error instanceof Error ? error.message : String(error) })
    } finally {
      setBusy('')
    }
  }, [load])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return memories
    return memories.filter(item => `${item.summary} ${item.memory_key} ${item.kind}`.toLowerCase().includes(needle))
  }, [memories, query])

  const memoryDiag = asRecord(status.memory)
  const relationship = asRecord(status.relationship)
  const life = asRecord(status.life)
  const relationshipValues = asRecord(relationship.relationship)
  const affectValues = asRecord(relationship.affect)
  const scheduleItems = Array.isArray(schedule.items) ? schedule.items as ScheduleItem[] : []

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-[1040px] mx-auto px-8 py-7">
        <div className="flex items-center gap-3 mb-5">
          <FluentIcon name="People" size={22} />
          <div className="flex-1">
            <h1 className="text-[20px] font-[700]" style={{ color: 'var(--text)' }}>Continuity</h1>
            <p className="text-[12px] mt-0.5" style={{ color: 'var(--muted)' }}>
              Host-owned memory, relationship, and simulated-life diagnostics. Memories and the relationship debug view follow the current chat session; life stays character-global. UI controls never open the Continuity database directly.
            </p>
          </div>
          <ActionButton onClick={() => { setFeedback(null); void load() }} disabled={!connected || Boolean(busy)}>{busy === 'refresh' ? 'Refreshing…' : 'Refresh'}</ActionButton>
        </div>

        {!connected ? <div className="rounded-lg p-3 mb-4 text-[12px]" style={{ background: '#FFF4CE', color: '#8A5414' }}>Backend disconnected.</div> : null}
        {feedback ? <div className="rounded-lg p-3 mb-4 text-[12px]" style={{ background: feedback.ok ? '#E8F5E9' : '#FDE7E9', color: feedback.ok ? '#107C10' : '#A4262C' }}>{feedback.text}</div> : null}

        <div className="flex gap-2 mb-5">
          {(['overview', 'memories', 'schedule'] as Tab[]).map(name => (
            <button key={name} onClick={() => setTab(name)} className="rounded-lg px-3 py-2 text-[12px] font-[650]" style={{ background: tab === name ? 'var(--selection-bg)' : 'transparent', color: tab === name ? 'var(--accent)' : 'var(--muted)', border: '1px solid var(--border)' }}>
              {name === 'overview' ? 'Overview' : name === 'memories' ? 'Memories' : 'Schedule'}
            </button>
          ))}
        </div>

        {tab === 'overview' ? <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
            <Metric label="Schema" value={Number(status.schema_version || 0)} detail="C7 keeps schema 6" />
            <Metric label="Hot" value={Number(memoryDiag.hot_memory_count || 0)} />
            <Metric label="Cold" value={Number(memoryDiag.cold_memory_count || 0)} />
            <Metric label="Archive" value={Number(memoryDiag.archive_memory_count || 0)} />
          </div>
          <div className="grid lg:grid-cols-2 gap-4">
            <section className="rounded-xl border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
              <h2 className="text-[14px] font-[700] mb-3" style={{ color: 'var(--text)' }}>Maintenance and indexes</h2>
              <div className="text-[12px] leading-6" style={{ color: 'var(--muted)' }}>
                Last maintenance: <span style={{ color: 'var(--text)' }}>{fmtTime(memoryDiag.last_maintenance_at)}</span><br />
                Duration: <span style={{ color: 'var(--text)' }}>{memoryDiag.last_maintenance_duration_ms ?? '—'} ms</span>
              </div>
              <div className="flex flex-wrap gap-2 mt-4">
                <ActionButton disabled={Boolean(busy)} onClick={() => void act('maintenance', () => send('continuity.maintenance.run', {}), 'Continuity maintenance completed.')}>Run maintenance</ActionButton>
                <ActionButton disabled={Boolean(busy)} onClick={() => void act('rebuild', () => send('continuity.index.rebuild', {}), 'Derived retrieval indexes rebuilt; semantic vectors will refill lazily.')}>Rebuild indexes</ActionButton>
              </div>
            </section>
            <section className="rounded-xl border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
              <h2 className="text-[14px] font-[700] mb-3" style={{ color: 'var(--text)' }}>Relationship debug</h2>
              <div className="grid grid-cols-2 gap-x-5 gap-y-2 text-[12px]">
                {Object.entries(relationshipValues).map(([key, value]) => <div key={key} className="flex justify-between gap-3"><span style={{ color: 'var(--muted)' }}>{key}</span><span style={{ color: 'var(--text)' }}>{Number(value).toFixed(3)}</span></div>)}
                {Object.entries(affectValues).map(([key, value]) => <div key={key} className="flex justify-between gap-3"><span style={{ color: 'var(--muted)' }}>{key}</span><span style={{ color: 'var(--text)' }}>{Number(value).toFixed(3)}</span></div>)}
              </div>
              <div className="text-[11px] mt-3" style={{ color: 'var(--muted)' }}>Scope {sessionScope || '—'} · Debug values are derived state, not Persona, Canon, Work, or permission authority.</div>
            </section>
            <section className="rounded-xl border p-4 lg:col-span-2" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
              <h2 className="text-[14px] font-[700] mb-2" style={{ color: 'var(--text)' }}>Character Life diagnostics</h2>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-[12px]" style={{ color: 'var(--muted)' }}>
                <span>Classification: <b style={{ color: 'var(--text)' }}>{String(life.source_class || 'simulated_life')}</b></span>
                <span>Schedules: <b style={{ color: 'var(--text)' }}>{Number(life.schedule_count || 0)}</b></span>
                <span>Active events: <b style={{ color: 'var(--text)' }}>{Number(life.active_event_count || 0)}</b></span>
                <span>Active threads: <b style={{ color: 'var(--text)' }}>{Number(life.active_thread_count || 0)}</b></span>
              </div>
            </section>
          </div>
        </> : null}

        {tab === 'memories' ? <section>
          <div className="flex gap-3 mb-3">
            <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search saved memories…" className="flex-1 rounded-lg px-3 py-2 text-[12px] outline-none" style={{ background: 'var(--surface)', color: 'var(--text)', border: '1px solid var(--border)' }} />
            <div className="text-[12px] self-center" style={{ color: 'var(--muted)' }}>{filtered.length} active · {sessionScope || 'no active session'}</div>
          </div>
          <div className="space-y-2">
            {filtered.map(item => <div key={item.id} className="rounded-xl border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
              <div className="flex gap-4 items-start">
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-[650] break-words" style={{ color: 'var(--text)' }}>{item.summary}</div>
                  <div className="text-[10.5px] mt-2 flex flex-wrap gap-x-3 gap-y-1" style={{ color: 'var(--muted)' }}>
                    <span>{item.kind}</span><span>{item.memory_key}</span><span>{item.retention_tier}</span><span>{item.priority_class}</span><span>updated {fmtTime(item.updated_at)}</span>
                  </div>
                </div>
                <div className="flex gap-2 shrink-0">
                  <ActionButton disabled={Boolean(busy)} onClick={() => void act(`pin:${item.id}`, () => send('continuity.memory.pin', { memory_id: item.id, pinned: !item.pinned }), item.pinned ? 'Memory unpinned.' : 'Memory pinned.')}>{item.pinned ? 'Unpin' : 'Pin'}</ActionButton>
                  <ActionButton danger disabled={Boolean(busy)} onClick={() => {
                    if (!window.confirm('Forget this memory? Its derived Relationship/Life effects linked by provenance will also be invalidated.')) return
                    void act(`forget:${item.id}`, () => send('continuity.memory.forget', { memory_id: item.id }), 'Memory forgotten and derived closure applied.')
                  }}>Forget</ActionButton>
                </div>
              </div>
            </div>)}
            {!filtered.length ? <div className="text-[12px] py-8 text-center" style={{ color: 'var(--muted)' }}>No active memories match this view.</div> : null}
          </div>
        </section> : null}

        {tab === 'schedule' ? <section>
          <div className="rounded-xl border p-4 mb-3" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
            <div className="flex items-center justify-between gap-4">
              <div>
                <h2 className="text-[14px] font-[700]" style={{ color: 'var(--text)' }}>Schedule inspector · {String(schedule.local_date || 'today')}</h2>
                <div className="text-[11px] mt-1" style={{ color: 'var(--muted)' }}>Everything on this page is explicitly SIMULATED_LIFE, not Canon or external fact.</div>
              </div>
              <span className="text-[10px] font-[700] rounded-full px-2.5 py-1" style={{ background: '#E8F5E9', color: '#107C10' }}>SIMULATED_LIFE</span>
            </div>
          </div>
          <div className="space-y-2">
            {scheduleItems.map(item => <div key={item.item_id} className="rounded-xl border px-4 py-3 flex gap-4 items-center" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
              <div className="w-6 text-[11px]" style={{ color: 'var(--muted)' }}>{item.ordinal + 1}</div>
              <div className="flex-1">
                <div className="text-[13px] font-[650]" style={{ color: 'var(--text)' }}>{item.title}</div>
                <div className="text-[10.5px] mt-1" style={{ color: 'var(--muted)' }}>{item.category} · {new Date(item.starts_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}–{new Date(item.ends_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}{item.thread_key ? ' · ongoing thread' : ''}</div>
              </div>
              <span className="text-[11px]" style={{ color: 'var(--muted)' }}>{item.status}</span>
            </div>)}
            {!scheduleItems.length ? <div className="text-[12px] py-8 text-center" style={{ color: 'var(--muted)' }}>No schedule is present for this date.</div> : null}
          </div>
        </section> : null}
      </div>
    </div>
  )
}
