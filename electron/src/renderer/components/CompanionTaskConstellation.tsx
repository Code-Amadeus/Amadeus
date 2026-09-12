import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { groupCompanionTasks, type CompanionTaskGroup, type FloatingCompanionTask } from './floatingCompanionState'
import { constellationLayout, crossDisplayBranchPath, familyTasks, projectBranchPath, taskBranchPath, type TaskPose, type OverviewSlot } from './companionConstellationLayout'
import { scenePoint, taskLayoutOffset } from './companionLayoutPreferences'
import { characterHeightFor, displayForPoint, inheritedPlacement, localWorkArea, nodeKey, placeSelection, readDesktopNodes, recoverNodeDelta } from './companionDesktopLayout'
import type { CompanionLayoutController } from './useCompanionLayout'
import { useCompanionDrag } from './useCompanionDrag'
import { backDisplay, companionDragSelection, companionGatherPlacements, companionLinkSource, composeDisplayFocus, displayFocusLayout, nodeDisplay, restackCompanionOverview, selectDisplayTask, type DisplayFocuses } from './companionDisplayFocus'
import { stackAttention, stackMetrics } from './companionStack'

const labels = { running: '进行中', attention: '需要你', blocked: '待处理', ready: '有结果了', idle: '' }
const MOTION_MS = 640
const branchLabel = (task: FloatingCompanionTask) => task.sourceKind === 'sidechat' ? '侧边对话' : task.sourceKind === 'subagent' ? '子代理任务' : ''

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
  layout: CompanionLayoutController
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

function TaskCard({ task, group, pose, stacked, pile, cycle, automatic, selectProject, selectTask, back, dragCard, ...props }: Props & {
  task: FloatingCompanionTask; group: CompanionTaskGroup; pose: TaskPose; stacked: boolean
  pile: FloatingCompanionTask[]; cycle: (direction: number) => void; automatic: () => void
  selectProject: () => void; selectTask: (id: string) => void; back: () => void
  dragCard?: (event: PointerEvent<HTMLElement>) => void
}) {
  const children = group.tasks.filter(child => child.parentTaskId === task.id)
  const editing = props.layout.edit !== 'locked'
  const open = () => { if (!editing) stacked ? selectProject() : selectTask(task.id) }
  return <article data-task-id={task.id} data-companion-hit
    onPointerDown={dragCard}
    onClickCapture={editing ? event => { event.preventDefault(); event.stopPropagation() } : undefined}
    onKeyDownCapture={editing ? event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation() } } : undefined}
    aria-hidden={pose.hidden ? true : undefined}
    aria-label={pose.depth ? `卡组第 ${pose.depth + 1} 层，共 ${pile.length} 项，点击散开` : undefined}
    role={pose.depth ? 'button' : undefined} tabIndex={pose.depth ? 0 : undefined}
    onKeyDown={pose.depth ? event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectProject() } } : undefined}
    className={`companion-thought-card phase-${task.phase} ${task.contextOnly || pose.compactParent ? 'is-context-node' : ''} ${pose.focused ? 'companion-focus-anchor' : ''} ${pose.mini ? 'is-miniature' : ''} ${pose.projectOverview ? 'is-project-overview' : ''} ${pose.depth ? 'is-stack-back' : ''} ${props.fading.has(task.key) ? 'is-fading' : ''}`}
    onClick={pose.depth ? selectProject : undefined}
    onMouseEnter={() => props.holdCard(task.id, true)} onMouseLeave={() => props.holdCard(task.id, false)}>
    {pose.mini ? <button className="companion-miniature-tile" title={`${task.title} · ${labels[task.phase]}${props.faults.has(task.key) ? ' · 语音不可用' : ''}`}
      aria-label={`展开任务：${task.title}`} onClick={open}><i /><span /><span /></button> : (task.contextOnly || pose.compactParent) && !pose.depth && !pose.focused ? <div className="companion-context-title" role={task.contextOnly ? undefined : 'button'} tabIndex={task.contextOnly ? undefined : 0}
      onClick={task.contextOnly ? undefined : open} onKeyDown={event => { if (!task.contextOnly && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); open() } }}>
      <span>{task.contextOnly ? '主对话' : labels[task.phase]}</span><strong title={task.title}>{task.title}</strong>
      <button aria-label={`打开主对话：${task.title}`} onClick={event => { event.stopPropagation(); void props.openTask(task) }}>↗</button></div> : pose.focused ? <div className="companion-focus-heading">
      <header><div className="companion-focus-identity"><span>{group.title}{task.parentTaskId ? ` › ${group.tasks.find(parent => parent.id === task.parentTaskId)?.title || '关联主对话'}` : ''}</span>
        <span className="companion-thought-state">{labels[task.phase]}{branchLabel(task) ? ` · ${branchLabel(task)}` : ''}</span>
      </div><button onClick={back} aria-label="返回上一层">×</button></header>
      <div className="companion-focus-title-row"><h2>{task.title}</h2><CardActions task={task} {...props} /></div>
    </div> : <>
    <div className="companion-thought-open" role="button" tabIndex={pose.depth ? undefined : 0}
      onClick={open}
      onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open() } }}
      aria-label={stacked ? `展开项目任务：${group.title}` : `展开任务：${task.title}`}>
      <span className="companion-thought-state">{labels[task.phase]}{branchLabel(task) ? ` · ${branchLabel(task)}` : ''}</span>
      <strong title={task.title}>{task.title}</strong>
        <div className="companion-thought-excerpt" inert><Markdown text={task.detail} /></div>
        {(!stacked && !pose.projectOverview || props.faults.has(task.key)) && <small>{props.faults.has(task.key) ? '语音不可用 · ' : ''}{children.length ? `${children.length} 个关联分支${children.some(child => child.phase === 'attention') ? ' · 需要你' : ' · 展开查看'}` : '展开查看'}</small>}
    </div>
    {stacked && !pose.depth && <nav className="companion-stack-controls" aria-label="切换置顶卡片">
      <button aria-label="上一张置顶卡片" onClick={() => cycle(-1)}>‹</button><span>{pile.findIndex(item => item.id === task.id) + 1} / {pile.length}</span>
      <button aria-label="下一张置顶卡片" onClick={() => cycle(1)}>›</button>
      {stackMetrics(pile.length).overflow > 0 && <span title="其余任务保留在卡组内">+{stackMetrics(pile.length).overflow} 层</span>}
      {props.layout.profile.stackFronts?.[pose.stackId!]?.manual !== false && props.layout.profile.stackFronts?.[pose.stackId!] && <button onClick={automatic}>自动</button>}
      {pile.some(item => ['attention', 'blocked'].includes(item.phase) && !props.acknowledged.has(item.key)) && <i title="卡组内有待处理事项" />}
    </nav>}
    <div inert={pose.mini || pose.depth > 0} className="companion-miniature-actions"><CardActions task={task} {...props} /></div>
    </>}
    {dragCard && !pose.depth && <button className="companion-card-drag" data-companion-hit aria-label={`拖动${stacked ? '卡组' : '任务'}：${stacked ? group.title : task.title}`}
      style={{ transform: `scale(${1 / pose.scale})`, transformOrigin: '100% 0' }}
      title={stacked ? '整张卡都可拖动，同屏卡组一起移动' : '整张卡都可拖动，主对话带走同屏分支'}>⠿</button>}
    {!task.contextOnly && !pose.compactParent && !pose.focused && !pose.mini && !pose.depth && <button className="companion-thought-dismiss" aria-label={`收起卡片：${task.title}`}
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
  useEffect(() => {
    props.holdCard(task.id, true)
    return () => props.holdCard(task.id, false)
  }, [task.id, props.holdCard])
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
        <header><span>关联分支 · {children.length}</span><small>连接到当前任务</small></header>
        {children.map(child => <button key={child.id} onClick={() => onSelectTask(child.id)}>
          <i /><span><strong>{child.title}</strong><small>{branchLabel(child)} · {labels[child.phase]} · 查看进展与结果</small></span><b>↗</b>
        </button>)}
      </section>}
      {history.length > 0 && <div className="companion-reading-label"><span>{branchLabel(task) || '主任务'}的进展 · {allProgress.length}</span>
        {history.length > 3 && <button data-companion-hit onClick={() => setShowAll(value => !value)}>{showAll ? '只看最近三条' : `更早 ${history.length - 3} 条`}</button>}
      </div>}
      {progress.map((item, index) => <div className="companion-reading-node" data-reading-node key={item.id} style={{ '--reading-offset': `${index % 2 ? 72 : 6}px` } as CSSProperties}>
        <ReadingCard id={item.id} label="进展" text={item.text} time={item.at} />
      </div>)}
    </div>
  </div>
}

export default function CompanionTaskConstellation(props: Props) {
  const groups = useMemo(() => groupCompanionTasks(props.tasks), [props.tasks])
  const [focuses, setFocuses] = useState<DisplayFocuses>({})
  const [projectPage, setProjectPage] = useState(0)
  const [menu, setMenu] = useState<{ projectId: string; taskId?: string; displayId: number; x: number; y: number } | null>(null)
  const [focusHeights, setFocusHeights] = useState<Record<number, number>>({})
  const lastDisplay = useRef(0)
  const [size, setSize] = useState({ width: 1032, height: 1820, characterTop: 750 })
  const drag = useCompanionDrag(props.layout.edit === 'cards')
  const viewport = useRef<HTMLElement>(null)
  const canvas = useRef<HTMLDivElement>(null)
  const pageGroups = groups.slice(projectPage * 5, projectPage * 5 + 5)
  const { profile, mode, edit, updateProfile, desktop } = props.layout
  const baseGeometry = useMemo(() => {
    const offsets = profile.offsets
    const pinned = new Set(pageGroups.filter(group => offsets.projects[group.id] || group.tasks.some(task => offsets.tasks[task.id])).map(group => group.id))
    const occupied = (slot: OverviewSlot) => {
      const group = pageGroups.find(group => group.id === slot.id)!
      const projectOffset = offsets.projects[slot.id] || { x: 0, y: 0 }
      const boxes = [{ x: 0, y: 0, width: slot.width, height: slot.height }]
      for (const node of slot.nodes || []) {
        if (node.hidden) continue
        const task = group.tasks.find(task => task.id === node.id)!
        const offset = taskLayoutOffset(task, group.tasks, offsets)
        boxes.push({ x: node.x + offset.x, y: node.y + 62 + offset.y, width: slot.cardWidth, height: 184 })
      }
      const left = Math.min(...boxes.map(box => box.x)), top = Math.min(...boxes.map(box => box.y))
      return { x: slot.x + projectOffset.x + left, y: slot.y + projectOffset.y + top,
        width: Math.max(...boxes.map(box => box.x + box.width)) - left, height: Math.max(...boxes.map(box => box.y + box.height)) - top }
    }
    return constellationLayout(pageGroups, size.width, size.height, size.characterTop, '', '', 112, profile.slots,
      { mode, seed: profile.seed, pinned, occupied })
  }, [props.tasks, projectPage, size, profile, mode])
  const slotsKey = JSON.stringify(baseGeometry.slots)
  useEffect(() => {
    // An initial empty backend snapshot must not erase saved anchors. Keep the
    // other project pages as well; reading poses never enter this cache.
    if (!baseGeometry.slots.length) return
    updateProfile(before => {
      const slots = new Map(before.slots.map(slot => [slot.id, slot]))
      for (const slot of baseGeometry.slots) slots.set(slot.id, slot)
      const next = [...slots.values()]
      return JSON.stringify(next) === JSON.stringify(before.slots) ? before : { ...before, slots: next }
    })
  }, [slotsKey, profile.slots, updateProfile])
  const characterBounds = useMemo(() => {
    if (!desktop) return undefined
    const point = scenePoint({ x: desktop.home.width * .26, y: size.characterTop + 28 }, profile.scene, desktop.home.width, desktop.home.height)
    return { ...point, width: desktop.home.width * .48 * profile.scene.scale,
      height: (desktop.home.height - size.characterTop - 28) * profile.scene.scale }
  }, [desktop, profile.scene, size.characterTop])
  const overview = useMemo(() => {
    const element = viewport.current
    const insetX = 24, insetY = 28
    const surfaceWidth = element?.parentElement?.clientWidth || size.width + 48
    const surfaceHeight = element?.parentElement?.clientHeight || size.height + 52
    const place = <T extends { x: number; y: number; scale: number; id: string }>(pose: T, projectId: string, isTask: boolean): T => {
      const projectOffset = profile.offsets.projects[projectId] || { x: 0, y: 0 }
      const task = isTask && props.tasks.find(task => task.id === pose.id)
      const offset = task ? taskLayoutOffset(task, props.tasks, profile.offsets) : { x: 0, y: 0 }
      const point = scenePoint({ x: pose.x + projectOffset.x + offset.x + insetX, y: pose.y + projectOffset.y + offset.y + insetY }, profile.scene, surfaceWidth, surfaceHeight)
      return { ...pose, x: point.x, y: point.y, scale: pose.scale * profile.scene.scale }
    }
    const projects = baseGeometry.projects.map(pose => place(pose, pose.id, false))
    const cards = baseGeometry.cards.map(pose => place(pose, pose.projectId, true))
    const inCanvas = <T extends { x: number; y: number }>(pose: T) => ({ ...pose, x: pose.x - insetX, y: pose.y - insetY })
    const placed = { ...baseGeometry, projects: projects.map(pose => inCanvas(inheritedPlacement(pose, 'projects', profile.placements, []))),
      cards: cards.map(pose => {
        const ancestors: { kind: 'tasks' | 'projects'; pose: typeof pose | typeof projects[number] }[] = []
        let task = props.tasks.find(task => task.id === pose.id)
        const visited = new Set<string>()
        while (task?.parentTaskId && !visited.has(task.parentTaskId)) {
          visited.add(task.parentTaskId)
          const parent = cards.find(card => card.id === task!.parentTaskId)
          if (parent) ancestors.push({ kind: 'tasks', pose: parent })
          task = props.tasks.find(candidate => candidate.id === task!.parentTaskId)
        }
        const project = projects.find(project => project.id === pose.projectId)
        if (project) ancestors.push({ kind: 'projects', pose: project })
        return inCanvas(inheritedPlacement(pose, 'tasks', profile.placements, ancestors))
      }) }
    return desktop ? restackCompanionOverview(placed, pageGroups, desktop, profile.stackFronts, props.acknowledged,
      characterBounds && inCanvas(characterBounds)) : placed
  }, [baseGeometry, profile, size, props.tasks, desktop, props.acknowledged, characterBounds])
  const views = useMemo(() => {
    if (!desktop) return []
    const characterDisplay = displayForPoint({ x: desktop.home.width / 2 + profile.scene.x,
      y: desktop.home.height + profile.scene.y - characterHeightFor(desktop.home) * profile.scene.scale / 2 }, desktop)
    const characterTop = scenePoint({ x: 0, y: size.characterTop + 28 }, profile.scene, desktop.home.width, desktop.home.height).y - 28
    return desktop.displays.flatMap(display => {
      const focus = focuses[display.id]
      if (!focus) return []
      const area = localWorkArea(display, desktop)
      const bottom = characterDisplay.id === display.id ? characterTop - area.y : area.height - 52
      const view = displayFocusLayout(overview, pageGroups, desktop, display.id, focus, mode, focusHeights[display.id] || 112, bottom, characterBounds || null)
      return view ? [view] : []
    })
  }, [overview, props.tasks, focuses, focusHeights, desktop, profile.scene, size.characterTop, mode, characterBounds])
  const geometry = useMemo(() => composeDisplayFocus(overview, views), [overview, views])
  useEffect(() => {
    updateProfile(before => {
      const stackFronts = { ...before.stackFronts }
      let changed = false
      for (const pose of overview.cards.filter(card => card.stackId && !card.depth)) {
        const pile = props.tasks.filter(task => overview.cards.some(card => card.id === task.id && card.stackId === pose.stackId))
        const previous = stackFronts[pose.stackId!], attention = stackAttention(pile, props.acknowledged)
        if (previous?.taskId === pose.id && previous.attention === attention) continue
        stackFronts[pose.stackId!] = { taskId: pose.id, attention,
          manual: previous?.taskId === pose.id ? previous.manual : false }; changed = true
      }
      return changed ? { ...before, stackFronts } : before
    })
  }, [overview, props.acknowledged, updateProfile])
  const layoutKey = JSON.stringify(geometry)
  const back = useCallback((displayId: number) => { setFocuses(current => backDisplay(current, displayId)) }, [])
  const displayForNode = (kind: 'projects' | 'cards', id: string, fallback = lastDisplay.current) => {
    const pose = overview[kind].find(pose => pose.id === id)
    return desktop && pose ? nodeDisplay(pose, desktop) : fallback
  }
  const selectProject = (id: string, displayId = displayForNode('projects', id)) => {
    if (edit !== 'locked') return
    lastDisplay.current = displayId
    setFocuses(current => {
      const selected = current[displayId]?.trail.at(-1)
      return selected?.projectId === id && !selected.taskId ? backDisplay(current, displayId)
        : selectDisplayTask(current, displayId, { projectId: id, taskId: '' })
    })
  }
  const gatherPlacements = useMemo(() => {
    if (!desktop || !characterBounds || !menu || menu.taskId || edit !== 'cards') return null
    return companionGatherPlacements(baseGeometry, overview, desktop, menu.projectId, characterBounds)
  }, [menu, edit, baseGeometry, overview, desktop, characterBounds])
  const gatherProject = (id: string) => {
    if (!gatherPlacements) return
    const nodes = [...overview.projects.map(pose => ({ ...pose, kind: 'projects' as const })),
      ...overview.cards.map(pose => ({ ...pose, kind: 'tasks' as const }))].map(node => ({ ...node, x: node.x + 24, y: node.y + 28 }))
    const selected = new Set([`projects:${id}`, ...overview.cards.filter(card => card.projectId === id).map(card => `tasks:${card.id}`)])
    updateProfile(before => ({ ...before, placements: placeSelection(before.placements, nodes, selected, node => gatherPlacements.get(nodeKey(node))!) }))
    setMenu(null)
  }
  const selectTask = (projectId: string, taskId: string, fallback?: number) => {
    if (edit !== 'locked') return
    const task = props.tasks.find(task => task.id === taskId)
    if (task?.contextOnly) { void props.openTask(task); return }
    const displayId = displayForNode('cards', taskId, fallback)
    lastDisplay.current = displayId
    setFocuses(current => selectDisplayTask(current, displayId, { projectId, taskId }, props.tasks.find(task => task.id === taskId)?.parentTaskId))
  }
  const openMenu = (event: React.MouseEvent<HTMLElement>, projectId: string, displayId: number, taskId?: string) => {
    event.preventDefault(); event.stopPropagation()
    const bounds = canvas.current!.getBoundingClientRect()
    const area = desktop && localWorkArea(desktop.displays.find(display => display.id === displayId)!, desktop)
    setMenu({ projectId, taskId, displayId,
      x: Math.max((area?.x || 0) - 16, Math.min(event.clientX - bounds.x, (area ? area.x + area.width : size.width) - 268)),
      y: Math.max((area?.y || 0) - 20, Math.min(event.clientY - bounds.y, (area ? area.y + area.height : size.height) - 218)) })
  }
  useEffect(() => {
    if (!menu) return
    const dismiss = (event: Event) => { if (!(event.target as Element)?.closest?.('.companion-project-menu')) setMenu(null) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenu(null) }
    window.addEventListener('pointerdown', dismiss); window.addEventListener('keydown', escape)
    return () => { window.removeEventListener('pointerdown', dismiss); window.removeEventListener('keydown', escape) }
  }, [menu])
  useLayoutEffect(() => {
    const element = viewport.current
    if (!element) return
    const measure = () => {
      const character = document.querySelector<HTMLElement>('.floating-companion-render')
      // Intrinsic scene geometry is unaffected by user zoom. The shared affine
      // transform is applied once, after automatic and manual node placement.
      const frame = element.parentElement!
      setSize({ width: frame.clientWidth - 48, height: frame.clientHeight - 52, characterTop: (character?.offsetTop ?? frame.clientHeight * .42) - 28 })
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element.parentElement!)
    measure()
    return () => observer.disconnect()
  }, [Boolean(props.tasks.length), desktop?.key])
  useEffect(() => { if (edit !== 'locked') setFocuses({}); setMenu(null) }, [edit])
  const beginNodeDrag = (event: PointerEvent<HTMLElement>, kind: 'projects' | 'tasks', id: string, stackId?: string) => {
    if (edit !== 'cards' || !desktop) return
    const selected = companionDragSelection(overview, groups, desktop, kind, id, stackId)
    const nodes = [...overview.projects.map(pose => ({ ...pose, kind: 'projects' as const })),
      ...overview.cards.map(pose => ({ ...pose, kind: 'tasks' as const }))]
      .map(node => ({ ...node, x: node.x + 24, y: node.y + 28 }))
    const initial = profile.placements
    const move = (delta: { x: number; y: number }) => updateProfile(before => ({ ...before,
      placements: placeSelection(initial, nodes, selected, node => ({ x: node.x + delta.x, y: node.y + delta.y, scale: node.scale })) }))
    drag.begin(event, delta => {
      move(delta)
    }, () => updateProfile(before => ({ ...before, placements: initial })), (delta, point) => {
      if (!desktop) return
      const display = displayForPoint({ x: point.x + desktop.bounds.x - desktop.home.x, y: point.y + desktop.bounds.y - desktop.home.y }, desktop)
      move(recoverNodeDelta(nodes.filter(node => selected.has(nodeKey(node))), delta, localWorkArea(display, desktop)))
    })
  }
  useEffect(() => {
    setFocuses(current => Object.fromEntries(Object.entries(current).flatMap(([id, focus]) => {
      const trail = focus.trail.filter(item => groups.some(group => group.id === item.projectId && (!item.taskId || group.tasks.some(task => task.id === item.taskId))))
      return trail.length && desktop?.displays.some(display => display.id === Number(id)) ? [[id, { ...focus, trail }]] : []
    })))
    setProjectPage(page => Math.min(page, Math.max(0, Math.ceil(groups.length / 5) - 1)))
  }, [groups, desktop?.key])
  const selectedKey = views.map(view => `${view.displayId}:${view.task?.id || ''}`).join('|')
  useLayoutEffect(() => {
    const headings = canvas.current?.querySelectorAll<HTMLElement>('.companion-focus-heading')
    if (!headings?.length) return
    // Natural title wrapping owns the heading height; the reading area consumes
    // all remaining space instead of reserving an empty fixed-height title card.
    const measure = () => setFocusHeights(before => {
      const next = { ...before }
      for (const heading of headings) next[Number(heading.closest<HTMLElement>('[data-display-id]')!.dataset.displayId)] = Math.ceil(heading.offsetHeight) + 2
      return Object.keys(next).every(id => next[Number(id)] === before[Number(id)]) ? before : next
    })
    const observer = new ResizeObserver(measure)
    headings.forEach(heading => observer.observe(heading))
    measure()
    return () => observer.disconnect()
  }, [selectedKey])
  useEffect(() => {
    if (!views.length) return
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') back(lastDisplay.current) }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [Boolean(views.length), back])
  useLayoutEffect(() => {
    const root = canvas.current
    if (!root) return
    const cardElements = new Map([...root.querySelectorAll<HTMLElement>('[data-task-id]')].map(element => [element.dataset.taskId!, element]))
    const projectElements = new Map([...root.querySelectorAll<HTMLElement>('[data-project-id]')].map(element => [element.dataset.projectId!, element]))
    const boxes = new Map([...cardElements.values(), ...projectElements.values()].map(element => [element, { left: 0, top: 0, right: 0, bottom: 0 }]))
    const obstacles = geometry.cards.filter(pose => !pose.hidden).flatMap(pose => {
      const element = cardElements.get(pose.id)
      return element ? [boxes.get(element)!] : []
    })
    const areaFor = (id: number) => { const area = localWorkArea(desktop!.displays.find(display => display.id === id)!, desktop!); return { ...area, x: area.x - 24, y: area.y - 28 } }
    const edges = [...root.querySelectorAll<SVGGElement>('[data-edge-task]')].map(edge => {
      const pose = geometry.cards.find(item => item.id === edge.dataset.edgeTask)!
      const source = companionLinkSource(pose, props.tasks, geometry.cards)
      const card = cardElements.get(pose.id)
      const from = source && (source.kind === 'cards' ? cardElements : projectElements).get(source.id)
      const sourceDisplay = desktop && source && displayForNode(source.kind, source.id)
      const targetDisplay = desktop && displayForNode('cards', pose.id)
      return { paths: [...edge.querySelectorAll('path')], a: from ? boxes.get(from) : null, b: card ? boxes.get(card) : null,
        parent: source?.kind === 'cards', reading: pose.focused,
        cross: sourceDisplay && targetDisplay && sourceDisplay !== targetDisplay ? [areaFor(sourceDisplay), areaFor(targetDisplay)] : null }
    })
    const started = performance.now()
    let frame = 0, lastHitSync = 0
    const update = () => {
      const bounds = root.getBoundingClientRect()
      for (const [element, box] of boxes) {
        const measured = element.getBoundingClientRect()
        box.left = measured.left - bounds.left; box.right = measured.right - bounds.left
        box.top = measured.top - bounds.top; box.bottom = measured.bottom - bounds.top
      }
      for (const edge of edges) {
        const { a, b } = edge
        let d = ''
        if (a && b) {
          const y = (a.top + a.bottom) / 2, ty = b.top + 30
          d = edge.cross ? crossDisplayBranchPath(a, b, edge.cross[0], edge.cross[1])
            : edge.parent ? taskBranchPath(a, b, obstacles)
            : edge.reading ? `M ${a.left} ${y} C ${a.left - 42} ${y}, ${b.right + 48} ${ty}, ${b.right} ${ty}`
            : projectBranchPath(a, b, obstacles)
        }
        for (const path of edge.paths) path.setAttribute('d', d)
      }
      if (performance.now() - lastHitSync > 32) { props.syncHitRegions(); lastHitSync = performance.now() }
      if (performance.now() - started < MOTION_MS + 220) frame = requestAnimationFrame(update)
    }
    frame = requestAnimationFrame(update)
    return () => cancelAnimationFrame(frame)
  }, [layoutKey, props.syncHitRegions])
  if (!props.tasks.length) return null
  return <>
    {drag.dragging && <div className="companion-drag-capture" data-companion-hit />}
    <section ref={viewport} className={`companion-constellation ${views.length ? 'has-display-reading' : ''} ${geometry.compact ? 'is-compact' : ''} ${edit !== 'locked' ? 'is-editing' : ''} ${edit === 'cards' ? 'is-editing-cards' : ''}`}
      aria-label="项目与任务" onScroll={props.syncHitRegions}>
      <div ref={canvas} className="companion-constellation-canvas" style={{ height: geometry.height }}
        onPointerDownCapture={event => {
          const element = (event.target as HTMLElement).closest<HTMLElement>('[data-display-id]')
          if (element) lastDisplay.current = Number(element.dataset.displayId)
        }}>
        {views.map(view => <button key={`dismiss:${view.displayId}`} className="companion-focus-dismiss" data-companion-hit data-display-id={view.displayId}
          style={{ left: view.area.x - 24, top: view.area.y - 28, width: view.area.width, height: view.area.height }}
          aria-label="返回上一层" onClick={() => back(view.displayId)} />)}
        {views.map(view => <div key={view.displayId} className="companion-display-reading" data-display-id={view.displayId}
          onContextMenu={event => openMenu(event, view.group.id, view.displayId, view.task?.id)}
          style={{ left: view.area.x, top: view.area.y, width: view.area.width - 48, height: view.area.height - 52,
            '--reading-width': `${view.geometry.readingWidth}px`, '--reading-top': `${view.geometry.readingTop}px`,
            '--reading-height': `${view.geometry.readingHeight}px` } as CSSProperties}>
          <button className="companion-return-overview" data-companion-hit onClick={() => back(view.displayId)}>{view.focus.trail.length > 1 ? '‹ 返回上一层' : '‹ 返回概览'}</button>
          {view.task && <TaskReading key={view.task.id} {...props} task={view.task} group={view.group} onSelectTask={id => selectTask(view.group.id, id, view.displayId)} />}
        </div>)}
        {!views.some(view => view.displayId === (desktop && displayForPoint({ x: 0, y: 0 }, desktop).id)) && groups.length > 5 && <button className="companion-project-pages" data-companion-hit onClick={() => setProjectPage(page => (page + 1) % Math.ceil(groups.length / 5))}>项目 {projectPage + 1} / {Math.ceil(groups.length / 5)} · 换一组 →</button>}
        {geometry.projects.map(pose => {
          const group = groups.find(item => item.id === pose.id)!
          return <div key={pose.id}><button data-project-id={pose.id} data-companion-hit data-display-id={displayForNode('projects', pose.id)}
            className={`companion-project-badge companion-node-position ${pose.mini ? 'is-rail-badge' : ''}`}
            style={{ width: pose.width, transform: `translate(${pose.x}px, ${pose.y}px) scale(${pose.scale})` }}
            onPointerDown={edit === 'cards' ? event => beginNodeDrag(event, 'projects', pose.id) : undefined}
            onContextMenu={event => openMenu(event, pose.id, displayForNode('projects', pose.id))}
            onClick={() => { if (!drag.consumeClick()) selectProject(pose.id) }}>
            <i className={group.tasks.some(task => task.phase === 'attention') ? 'needs-you' : ''} />
            <span title={group.title}>{group.title}</span><small>{pose.count} 个任务{group.tasks.some(t => t.sourceKind === 'sidechat' && !t.contextOnly) ? ` · 含 ${group.tasks.filter(t => t.sourceKind === 'sidechat' && !t.contextOnly).length} 侧边` : ''}</small>
          </button></div>
        })}
        <svg className="companion-thought-links" aria-hidden="true" style={{ '--link-scale': profile.scene.scale } as CSSProperties}>
          {geometry.cards.filter(pose => !pose.hidden && !pose.depth).map(pose => <g key={pose.id} data-edge-task={pose.id} data-edge-project={pose.projectId}>
            <SignalPaths paths={['']} />
          </g>)}
        </svg>
        {geometry.cards.map((pose, index) => {
          const group = groups.find(item => item.id === pose.projectId)!
          const task = group.tasks.find(item => item.id === pose.id)!
          const view = views.find(view => view.taskIds.has(pose.id))
          const displayId = displayForNode('cards', pose.id, view?.displayId)
          const pile = pose.stackId ? group.tasks.filter(task => geometry.cards.some(card => card.id === task.id && card.stackId === pose.stackId)).sort((a,b) => a.id.localeCompare(b.id)) : []
          const cycle = (direction: number) => {
            const next = pile[(pile.findIndex(item => item.id === task.id) + direction + pile.length) % pile.length]
            if (next) updateProfile(before => ({ ...before, stackFronts: { ...before.stackFronts,
              [pose.stackId!]: { taskId: next.id, attention: stackAttention(pile, props.acknowledged), manual: true } } }))
          }
          const automatic = () => updateProfile(before => {
            const stackFronts = { ...before.stackFronts }; delete stackFronts[pose.stackId!]
            return { ...before, stackFronts }
          })
          return <div key={pose.id} className={`companion-task-position companion-node-position ${pose.hidden ? 'is-hidden' : ''}`} data-display-id={displayId}
            onContextMenu={event => openMenu(event, group.id, displayId, task.id)}
            style={{ width: pose.width, height: pose.height, transform: `translate(${pose.x}px, ${pose.y}px) scale(${pose.scale})`,
              zIndex: pose.focused ? 50 : 40 - Math.min(pose.depth, 7), '--motion-delay': `${index % 5 * 24}ms` } as CSSProperties}>
            <TaskCard {...props} task={task} group={group} pose={pose} stacked={pile.length > 1} pile={pile} cycle={cycle} automatic={automatic}
              dragCard={edit === 'cards' ? event => beginNodeDrag(event, 'tasks', task.id, pose.stackId) : undefined}
              selectProject={() => selectProject(group.id, displayId)} selectTask={id => selectTask(group.id, id, displayId)} back={() => back(displayId)} />
          </div>
        })}
        {menu && <div className="companion-project-menu" data-companion-hit role="menu" aria-label="卡片操作" style={{ left: menu.x, top: menu.y }}>
          {edit !== 'locked' ? <>
            {!menu.taskId && edit === 'cards' && <><button role="menuitem" disabled={!gatherPlacements} onClick={() => gatherProject(menu.projectId)}>将整个项目收拢到此屏</button>
              {!gatherPlacements && <small>此屏空位不足，请先腾出一些空间。</small>}</>}
            <button role="menuitem" onClick={() => props.layout.setEdit('locked')}>完成调整 · 自动保存</button>
          </> : <>
            {focuses[menu.displayId] ? <>
              <button role="menuitem" onClick={() => { back(menu.displayId); setMenu(null) }}>{focuses[menu.displayId].trail.at(-1)?.taskId ? '收起详情 · 返回上一层' : '收起卡组 · 返回概览'}</button>
              {focuses[menu.displayId].trail.length > 1 && <button role="menuitem" onClick={() => { setFocuses(before => { const next = { ...before }; delete next[menu.displayId]; return next }); setMenu(null) }}>返回本屏概览</button>}
            </> : <button role="menuitem" onClick={() => { selectProject(menu.projectId, menu.displayId); setMenu(null) }}>散开本屏项目卡片</button>}
            <button role="menuitem" onClick={() => { props.layout.setPanelOpen(true); props.layout.setEdit('cards') }}>编辑卡片位置</button>
          </>}
          <button role="menuitem" onClick={() => setMenu(null)}>关闭菜单</button>
        </div>}
      </div>
    </section>
  </>
}
