import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { groupCompanionTasks, type CompanionTaskGroup, type FloatingCompanionTask } from './floatingCompanionState'
import { constellationLayout, rootTasks, type TaskPose, type OverviewSlot } from './companionConstellationLayout'

const labels = { running: '进行中', attention: '需要你', blocked: '待处理', ready: '有结果了', idle: '' }
const MOTION_MS = 640

type Props = {
  tasks: FloatingCompanionTask[]
  fading: Map<string, number>
  faults: Set<string>
  acknowledged: Set<string>
  speakingKey: string
  openTask: (task: FloatingCompanionTask) => unknown
  dismissCard: (id: string) => void
  acknowledge: (key: string) => void
  holdCard: (id: string, held: boolean) => void
  syncHitRegions: () => void
}

function SignalPaths({ paths }: { paths: string[] }) {
  return <>{paths.map((d, i) => <g key={i}>
    <path className="companion-link-shadow" d={d} />
    <path className="companion-link-base" d={d} />
    <path className="companion-link-signal" d={d} pathLength="100" style={{ animationDelay: `${i * -1.7}s` }} />
  </g>)}</>
}

function Markdown({ text, preview = false }: { text: string; preview?: boolean }) {
  return <div className={`companion-markdown ${preview ? 'is-preview' : ''}`}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
      table: ({ children }) => <div className="companion-table-scroll"><table>{children}</table></div>,
      a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
    }}>{text}</ReactMarkdown>
  </div>
}

function CardActions({ task, acknowledged, acknowledge, openTask }: Pick<Props, 'acknowledged' | 'acknowledge' | 'openTask'> & { task: FloatingCompanionTask }) {
  return <footer className="companion-card-actions">
    {task.repeatable && !acknowledged.has(task.key) && <button className="companion-acknowledge" onClick={() => acknowledge(task.key)}>知道了</button>}
    <button className="companion-open-source" onClick={() => void openTask(task)}>{task.codexThreadId ? '打开 Codex' : '打开 Amadeus'} ↗</button>
  </footer>
}

function TaskCard({ task, group, pose, stacked, selectProject, selectTask, back, ...props }: Props & {
  task: FloatingCompanionTask; group: CompanionTaskGroup; pose: TaskPose; stacked: boolean
  selectProject: () => void; selectTask: (id: string) => void; back: () => void
}) {
  const children = group.tasks.filter(child => child.parentTaskId === task.id)
  const open = () => stacked ? selectProject() : selectTask(task.id)
  return <article data-task-id={task.id} data-companion-hit
    aria-hidden={pose.depth > 0 || pose.hidden ? true : undefined}
    className={`companion-thought-card phase-${task.phase} ${pose.focused ? 'companion-focus-anchor' : ''} ${pose.mini ? 'is-miniature' : ''} ${pose.depth ? 'is-stack-back' : ''} ${props.fading.has(task.key) ? 'is-fading' : ''}`}
    onClick={pose.depth ? selectProject : undefined}
    onMouseEnter={() => props.holdCard(task.id, true)} onMouseLeave={() => props.holdCard(task.id, false)}>
    {pose.focused ? <div className="companion-focus-heading">
      <header><div className="companion-focus-identity"><span>{group.title}</span>
        <span className="companion-thought-state">{labels[task.phase]}{task.sourceKind === 'subagent' ? ' · 子代理任务' : ''}</span>
      </div><button onClick={back} aria-label="返回上一层">×</button></header>
      <div className="companion-focus-title-row"><h2>{task.title}</h2><CardActions task={task} {...props} /></div>
    </div> : <>
    <div className="companion-thought-open" role="button" tabIndex={pose.depth ? undefined : 0}
      onClick={open}
      onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open() } }}
      aria-label={stacked ? `展开项目任务：${group.title}` : `展开任务：${task.title}`}>
      <span className="companion-thought-state">{labels[task.phase]}{task.sourceKind === 'subagent' ? ' · 子代理任务' : ''}</span>
      <strong title={task.title}>{task.title}</strong>
        <div className="companion-thought-excerpt" inert><Markdown text={task.detail} /></div>
        <small>{props.faults.has(task.key) ? '语音不可用 · ' : ''}{stacked ? `叠放 ${rootTasks(group).length} 个任务 · 点击散开` : children.length ? `${children.length} 个子代理 · 展开查看` : '展开查看'}</small>
    </div>
    <div inert={pose.mini || pose.depth > 0} className="companion-miniature-actions"><CardActions task={task} {...props} /></div>
    </>}
    {!pose.focused && !pose.mini && !pose.depth && <button className="companion-thought-dismiss" aria-label={`收起卡片：${task.title}`}
      title="收起卡片，新活动时恢复" onClick={() => props.dismissCard(task.id)}>×</button>}
  </article>
}

function ReadingCard({ id, label, text, time, current = false }: { id: string; label: string; text: string; time?: number; current?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const [clipped, setClipped] = useState(false)
  const ref = useRef<HTMLElement>(null)
  useLayoutEffect(() => {
    const content = ref.current?.querySelector<HTMLElement>('.companion-markdown')
    if (!content) return
    const measure = () => { if (!expanded) setClipped(content.scrollHeight > content.clientHeight + 1) }
    const observer = new ResizeObserver(measure)
    observer.observe(content)
    measure()
    return () => observer.disconnect()
  }, [text, expanded])
  return <article ref={ref} className={`companion-reading-card ${current ? 'is-current' : ''} ${expanded ? 'is-expanded' : ''} ${clipped ? '' : 'is-short'}`} data-reading-id={id} data-companion-hit>
    <header><span>{label}</span>{time ? <time>{new Date(time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time> : null}</header>
    <Markdown text={text} preview={!expanded} />
    {(clipped || expanded) && <button className="companion-reading-toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起全文' : '展开全文'}</button>}
  </article>
}

function TaskReading({ task, group, onSelectTask, ...props }: Props & {
  task: FloatingCompanionTask; group: CompanionTaskGroup; onSelectTask: (id: string) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [showAll, setShowAll] = useState(false)
  const [links, setLinks] = useState<string[]>([])
  const allProgress = (task.activities || []).filter(item => item.kind === 'progress')
  // The running headline is already the latest progress. Never show it twice.
  const history = allProgress.filter((item, index) => !(index === allProgress.length - 1 && item.text === task.detail))
  const progress = showAll ? history : history.slice(-3)
  const children = group.tasks.filter(child => child.parentTaskId === task.id)
  const parent = group.tasks.find(candidate => candidate.id === task.parentTaskId)
  useLayoutEffect(() => {
    const root = ref.current
    if (!root) return
    const measure = () => {
      const boxes = [...root.querySelectorAll<HTMLElement>('[data-reading-node]')]
      const next = boxes.slice(1).map((box, i) => {
        const previous = boxes[i]
        const x = previous.offsetLeft + 20, y = previous.offsetTop + previous.offsetHeight
        const tx = box.offsetLeft + 20, ty = box.offsetTop
        return `M ${x} ${y} C ${x} ${y + 18}, ${tx} ${ty - 18}, ${tx} ${ty}`
      })
      const current = root.querySelector<HTMLElement>('.is-current-node')
      const branches = [...root.querySelectorAll<HTMLElement>('.companion-child-thoughts>button')]
      if (current) for (const branch of branches) {
        const section = branch.parentElement!
        const x = current.offsetLeft + 20, y = current.offsetTop + current.offsetHeight
        const tx = section.offsetLeft + branch.offsetLeft, ty = section.offsetTop + branch.offsetTop + branch.offsetHeight / 2
        next.push(`M ${x} ${y} C ${x} ${ty}, ${tx - 24} ${ty}, ${tx} ${ty}`)
      }
      setLinks(next)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(root)
    root.querySelectorAll('[data-reading-node]').forEach(element => observer.observe(element))
    measure()
    return () => observer.disconnect()
  }, [task.activities, task.detail, showAll, children.length])
  return <div className="companion-focus-panel" onScroll={props.syncHitRegions}>
    <div ref={ref} className="companion-reading-trail">
      <svg className="companion-reading-links" aria-hidden="true"><SignalPaths paths={links} /></svg>
      {parent && <button className="companion-parent-task" data-companion-hit onClick={() => onSelectTask(parent.id)}>返回主任务：{parent.title} ↗</button>}
      {props.faults.has(task.key) && <p className="companion-fault">语音不可用 · 请查看原文</p>}
      <div className="companion-reading-node is-current-node" data-reading-node style={{ '--reading-offset': '12px' } as CSSProperties}>
        <ReadingCard key={task.key} id="current" current label={task.phase === 'ready' ? '最终回复' : task.phase === 'attention' ? '需要你确认' : '当前进展'} text={task.detail} />
      </div>
      {children.length > 0 && <section className="companion-child-thoughts" data-companion-hit>
        <header><span>子代理任务 · {children.length}</span><small>独立执行的分支</small></header>
        {children.map(child => <button key={child.id} onClick={() => onSelectTask(child.id)}>
          <i /><span><strong>{child.title}</strong><small>{labels[child.phase]} · 点击查看它的进展与结果</small></span><b>↗</b>
        </button>)}
      </section>}
      {history.length > 0 && <div className="companion-reading-label"><span>{task.sourceKind === 'subagent' ? '子代理的进展' : '主任务的进展'} · {allProgress.length}</span>
        {history.length > 3 && <button data-companion-hit onClick={() => setShowAll(value => !value)}>{showAll ? '只看最近三条' : `更早 ${history.length - 3} 条`}</button>}
      </div>}
      {progress.map((item, index) => <div className="companion-reading-node" data-reading-node key={item.id} style={{ '--reading-offset': `${index % 2 ? 72 : 6}px` } as CSSProperties}>
        <ReadingCard id={item.id} label="进展" text={item.text} time={item.at} />
      </div>)}
    </div>
  </div>
}

type Selection = { projectId: string; taskId: string }

export default function CompanionTaskConstellation(props: Props) {
  const groups = useMemo(() => groupCompanionTasks(props.tasks), [props.tasks])
  const [trail, setTrail] = useState<Selection[]>([])
  const [projectPage, setProjectPage] = useState(0)
  const [taskPage, setTaskPage] = useState(0)
  const [focusHeight, setFocusHeight] = useState(112)
  const [size, setSize] = useState({ width: 1032, height: 1820, characterTop: 750 })
  const viewport = useRef<HTMLElement>(null)
  const canvas = useRef<HTMLDivElement>(null)
  const selection = trail.at(-1)
  const selectedGroup = groups.find(group => group.id === selection?.projectId)
  const selectedTask = selectedGroup?.tasks.find(task => task.id === selection?.taskId)
  const activeProjectPage = selectedGroup ? Math.floor(groups.indexOf(selectedGroup) / 5) : projectPage
  const pageGroups = groups.slice(activeProjectPage * 5, activeProjectPage * 5 + 5)
  const focused = Boolean(selectedGroup)
  const overview = useRef<OverviewSlot[]>([])
  const geometry = constellationLayout(pageGroups, size.width, size.height, size.characterTop,
    selectedGroup?.id || '', selectedTask?.id || '', taskPage, focusHeight, overview.current)
  useLayoutEffect(() => { overview.current = geometry.slots }, [geometry.slots])
  const layoutKey = JSON.stringify(geometry)
  const back = useCallback(() => { setTrail(current => current.slice(0, -1)); viewport.current?.scrollTo({ top: 0 }) }, [])
  const selectProject = (id: string) => { setTrail([{ projectId: id, taskId: '' }]); setTaskPage(0) }
  const selectTask = (projectId: string, taskId: string) => {
    setTrail(current => {
      const latest = current.at(-1)
      if (latest?.projectId === projectId && !latest.taskId) return [...current, { projectId, taskId }]
      const child = props.tasks.find(task => task.id === taskId)
      if (latest?.taskId && child?.parentTaskId === latest.taskId) return [...current, { projectId, taskId }]
      if (current.length > 1 && current.at(-2)?.taskId === taskId) return current.slice(0, -1)
      return latest?.taskId ? [...current.slice(0, -1), { projectId, taskId }] : [{ projectId, taskId }]
    })
  }
  useLayoutEffect(() => {
    const element = viewport.current
    if (!element) return
    const measure = () => {
      const bounds = element.getBoundingClientRect()
      const character = document.querySelector('.floating-companion-render')?.getBoundingClientRect()
      setSize({ width: element.clientWidth, height: element.clientHeight, characterTop: (character?.top ?? innerHeight * .42) - bounds.top })
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    measure()
    return () => observer.disconnect()
  }, [Boolean(props.tasks.length)])
  useEffect(() => {
    setTrail(current => current.filter(item => groups.some(group => group.id === item.projectId && (!item.taskId || group.tasks.some(task => task.id === item.taskId)))))
    setProjectPage(page => Math.min(page, Math.max(0, Math.ceil(groups.length / 5) - 1)))
  }, [groups])
  useLayoutEffect(() => {
    const heading = canvas.current?.querySelector<HTMLElement>('.companion-focus-heading')
    if (!heading) return
    // Natural title wrapping owns the heading height; the reading area consumes
    // all remaining space instead of reserving an empty fixed-height title card.
    const measure = () => setFocusHeight(Math.ceil(heading.offsetHeight) + 2)
    const observer = new ResizeObserver(measure)
    observer.observe(heading)
    measure()
    return () => observer.disconnect()
  }, [selectedTask?.id])
  useEffect(() => {
    if (!selectedTask) return
    props.holdCard(selectedTask.id, true)
    return () => props.holdCard(selectedTask.id, false)
  }, [selectedTask?.id, props.holdCard])
  useEffect(() => {
    if (!focused) return
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') back() }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [focused, back])
  useLayoutEffect(() => {
    // Read the independently moving nodes. One task owns exactly one project
    // edge, including while it moves between the desktop and the reading area.
    const started = performance.now()
    let frame = 0, lastHitSync = 0
    const update = () => {
      const root = canvas.current
      if (!root) return
      const bounds = root.getBoundingClientRect()
      for (const edge of root.querySelectorAll<SVGGElement>('[data-edge-task]')) {
        const card = [...root.querySelectorAll<HTMLElement>('[data-task-id]')].find(item => item.dataset.taskId === edge.dataset.edgeTask)
        const task = props.tasks.find(item => item.id === edge.dataset.edgeTask)
        const parent = task?.parentTaskId && [...root.querySelectorAll<HTMLElement>('[data-task-id]')].find(item => item.dataset.taskId === task.parentTaskId)
        const badge = [...root.querySelectorAll<HTMLElement>('[data-project-id]')].find(item => item.dataset.projectId === edge.dataset.edgeProject)
        const from = parent || badge
        if (!card || !from) continue
        const a = from.getBoundingClientRect(), b = card.getBoundingClientRect()
        const reading = card.classList.contains('companion-focus-anchor')
        const x = (reading ? a.left : a.left + a.width * .5) - bounds.left
        const y = (reading ? a.top + a.height * .5 : a.bottom) - bounds.top
        const tx = (reading ? b.right : b.left + b.width * .45) - bounds.left
        const ty = (reading ? b.top + 30 : b.top) - bounds.top
        const d = reading ? `M ${x} ${y} C ${x - 42} ${y}, ${tx + 48} ${ty}, ${tx} ${ty}`
          : `M ${x} ${y} C ${x} ${y + 22}, ${tx} ${ty - 28}, ${tx} ${ty}`
        for (const path of edge.querySelectorAll('path')) path.setAttribute('d', d)
      }
      if (performance.now() - lastHitSync > 32) { props.syncHitRegions(); lastHitSync = performance.now() }
      if (performance.now() - started < MOTION_MS + 220) frame = requestAnimationFrame(update)
    }
    frame = requestAnimationFrame(update)
    return () => cancelAnimationFrame(frame)
  }, [layoutKey, props.syncHitRegions])
  if (!props.tasks.length) return null
  return <>
    {focused && <button className="companion-focus-dismiss" data-companion-hit aria-label="返回上一层" onClick={back} />}
    <section ref={viewport} className={`companion-constellation ${focused ? 'is-focused' : ''} ${geometry.compact ? 'is-compact' : ''}`}
      aria-label="项目与任务" onScroll={props.syncHitRegions}>
      <div ref={canvas} className="companion-constellation-canvas" style={{ height: geometry.height, '--reading-width': `${geometry.readingWidth}px`,
        '--reading-top': `${geometry.readingTop}px`, '--reading-height': `${geometry.readingHeight}px` } as CSSProperties}>
        {focused && <button className="companion-return-overview" data-companion-hit onClick={back}>{trail.length > 1 ? '‹ 返回上一层' : '‹ 返回概览'}</button>}
        {!focused && groups.length > 5 && <button className="companion-project-pages" data-companion-hit onClick={() => setProjectPage(page => (page + 1) % Math.ceil(groups.length / 5))}>项目 {projectPage + 1} / {Math.ceil(groups.length / 5)} · 换一组 →</button>}
        {geometry.projects.map(pose => {
          const group = groups.find(item => item.id === pose.id)!
          return <button key={pose.id} data-project-id={pose.id} data-companion-hit
            className={`companion-project-badge companion-node-position ${pose.mini ? 'is-rail-badge' : ''}`}
            style={{ width: pose.width, transform: `translate(${pose.x}px, ${pose.y}px)` }} onClick={() => selectProject(pose.id)}>
            <i className={group.tasks.some(task => task.phase === 'attention') ? 'needs-you' : ''} />
            <span title={group.title}>{group.title}</span><small>{pose.count} 个任务</small>
          </button>
        })}
        <svg className="companion-thought-links" aria-hidden="true">
          {geometry.cards.filter(pose => !pose.hidden && !pose.depth).map(pose => <g key={pose.id} data-edge-task={pose.id} data-edge-project={pose.projectId}>
            <SignalPaths paths={['']} />
          </g>)}
        </svg>
        {geometry.cards.map((pose, index) => {
          const group = groups.find(item => item.id === pose.projectId)!
          const task = group.tasks.find(item => item.id === pose.id)!
          const project = geometry.projects.find(item => item.id === pose.projectId)!
          return <div key={pose.id} className={`companion-task-position companion-node-position ${pose.hidden ? 'is-hidden' : ''}`}
            style={{ width: pose.width, height: pose.height, transform: `translate(${pose.x}px, ${pose.y}px) scale(${pose.scale})`,
              zIndex: pose.focused ? 5 : 4 - pose.depth, '--motion-delay': `${index % 5 * 24}ms` } as CSSProperties}>
            <TaskCard {...props} task={task} group={group} pose={pose} stacked={project.stacked}
              selectProject={() => selectProject(group.id)} selectTask={id => selectTask(group.id, id)} back={back} />
          </div>
        })}
        {focused && !selectedTask && geometry.taskPages > 1 && <button className="companion-project-task-pages" data-companion-hit
          style={{ top: size.characterTop - 54 }} onClick={() => setTaskPage((geometry.taskPage + 1) % geometry.taskPages)}>任务 {geometry.taskPage + 1} / {geometry.taskPages} · 下一组 →</button>}
        {selectedTask && <TaskReading key={selectedTask.id} {...props} task={selectedTask} group={selectedGroup!} onSelectTask={id => selectTask(selectedGroup!.id, id)} />}
      </div>
    </section>
  </>
}
