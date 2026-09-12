import { constellationLayout, type ProjectPose, type TaskPose } from './companionConstellationLayout.ts'
import { displayForPoint, localWorkArea } from './companionDesktopLayout.ts'
import type { CompanionDesktop, LayoutMode } from './companionLayoutPreferences'
import type { CompanionTaskGroup } from './floatingCompanionState'
import { stackOrder, STACK_LAYER_LIMIT, STACK_STEP, type StackSelection } from './companionStack.ts'

export type Selection = { projectId: string; taskId: string }
export type DisplayFocus = { trail: Selection[] }
export type DisplayFocuses = Record<number, DisplayFocus>
export type OverviewGeometry = ReturnType<typeof constellationLayout>
type Box = { x: number; y: number; width: number; height: number }
const overlaps = (a: Box, b: Box, gap = 0) => a.x < b.x + b.width + gap && b.x < a.x + a.width + gap
  && a.y < b.y + b.height + gap && b.y < a.y + a.height + gap
const nodeBox = (node: ProjectPose | TaskPose): Box => ({ x: node.x, y: node.y, width: node.width * node.scale, height: node.height * node.scale })

/** Ownership always comes from saved overview coordinates, never from a
 * temporary reading/thumbnail pose. Canvas inset is relative to the home frame. */
export function nodeDisplay(pose: ProjectPose | TaskPose, desktop: CompanionDesktop) {
  return displayForPoint({ x: pose.x + 24 + pose.width * pose.scale / 2,
    y: pose.y + 28 + pose.height * pose.scale / 2 }, desktop).id
}

/** Resolve piles after saved placement; detached cards remain independent. */
export function restackCompanionOverview(overview: OverviewGeometry, groups: CompanionTaskGroup[], desktop: CompanionDesktop,
  fronts: Record<string, StackSelection> = {}, acknowledged = new Set<string>(), character?: Box): OverviewGeometry {
  const cards = overview.cards.map(card => ({ ...card }))
  const scopes = new Set(cards.map(card => card.stackId).filter(Boolean))
  for (const scope of scopes) {
    const remaining = cards.filter(card => card.stackId === scope)
    while (remaining.length) {
      const first = remaining.shift()!, displayId = nodeDisplay(first, desktop)
      const pile = [first]
      for (let i = remaining.length - 1; i >= 0; i--) {
        const card = remaining[i]
        if (nodeDisplay(card, desktop) === displayId && Math.abs(card.x - first.x) <= first.width * first.scale * .5
          && Math.abs(card.y - first.y) <= (STACK_STEP * (STACK_LAYER_LIMIT - 1) + 36) * first.scale) pile.push(...remaining.splice(i, 1))
      }
      if (pile.length === 1) { Object.assign(first, { depth: 0, hidden: false, stackId: undefined }); continue }
      const id = `${scope}@${displayId}:${pile.map(card => card.id).sort()[0]}`
      const tasks = groups.flatMap(group => group.tasks).filter(task => pile.some(card => card.id === task.id))
      const ordered = stackOrder(tasks, fronts[id], acknowledged)
      const x = Math.min(...pile.map(card => card.x)), y = Math.min(...pile.map(card => card.y))
      for (const [index, task] of ordered.entries()) {
        const card = pile.find(card => card.id === task.id)!, depth = Math.min(index, STACK_LAYER_LIMIT - 1)
        Object.assign(card, { x: x + depth * 3 * first.scale, y: y + depth * STACK_STEP * first.scale,
          width: first.width, height: 184, scale: first.scale, depth: index, hidden: index >= STACK_LAYER_LIMIT,
          stackId: id })
      }
    }
  }
  // Older saved piles can put an ancestor underneath its children. Keep their
  // saved placements and display ownership; expose only the compact endpoint
  // in the closest free gap. Never reset the user's whole project layout.
  const tasks = groups.flatMap(group => group.tasks)
  for (const parent of cards.filter(card => card.compactParent && !card.hidden)) {
    const displayId = nodeDisplay(parent, desktop), box = nodeBox(parent)
    const children = cards.filter(card => !card.hidden && nodeDisplay(card, desktop) === displayId
      && tasks.some(task => task.id === card.id && task.parentTaskId === parent.id))
    if (!children.some(child => overlaps(box, nodeBox(child)))) continue
    const area = localWorkArea(desktop.displays.find(display => display.id === displayId)!, desktop)
    const obstacles = [...overview.projects, ...cards.filter(card => card.id !== parent.id && !card.hidden)]
      .filter(node => nodeDisplay(node, desktop) === displayId).map(nodeBox)
    if (character) obstacles.push(character)
    const left = area.x - 24 + 12, top = area.y - 28 + 12
    const right = area.x - 24 + area.width - box.width - 12, bottom = area.y - 28 + area.height - box.height - 12
    const xs = new Set([left, right, Math.max(left, Math.min(right, box.x)),
      ...obstacles.flatMap(other => [other.x - box.width - 12, other.x + other.width + 12])])
    const ys = new Set([top, bottom, Math.max(top, Math.min(bottom, box.y)),
      ...obstacles.flatMap(other => [other.y - box.height - 12, other.y + other.height + 12])])
    let best: { x: number; y: number; distance: number } | undefined
    for (const x of xs) for (const y of ys) {
      if (x < left || x > right || y < top || y > bottom) continue
      const distance = Math.hypot(x - box.x, y - box.y)
      if (best && distance >= best.distance || obstacles.some(other => overlaps({ ...box, x, y }, other, 12))) continue
      best = { x, y, distance }
    }
    if (best) { parent.x = best.x; parent.y = best.y }
  }
  return { ...overview, cards }
}

/** Moving membership is display-local; project/parent identity stays global. */
export function companionDragSelection(overview: OverviewGeometry, groups: CompanionTaskGroup[], desktop: CompanionDesktop,
  kind: 'projects' | 'tasks', id: string, stackId?: string): Set<string> {
  const origin = (kind === 'projects' ? overview.projects : overview.cards).find(node => node.id === id)
  if (!origin) return new Set()
  const displayId = nodeDisplay(origin, desktop), tasks = new Map(groups.flatMap(group => group.tasks).map(task => [task.id, task]))
  const belongs = (taskId: string) => {
    const seen = new Set<string>()
    while (!seen.has(taskId)) {
      if (taskId === id) return true
      seen.add(taskId)
      const parent = tasks.get(taskId)?.parentTaskId
      if (!parent) break
      taskId = parent
    }
    return false
  }
  const selected = new Set(overview.cards.filter(node => nodeDisplay(node, desktop) === displayId
    && (stackId ? node.stackId === stackId : kind === 'projects' ? node.projectId === id : belongs(node.id)))
    .map(node => `tasks:${node.id}`))
  if (kind === 'projects') selected.add(`projects:${id}`)
  return selected
}

/** Parent identity is never substituted by a pile representative. Collapsing
 * owns visibility and must keep that endpoint visible in the first place. */
export function companionLinkSource(card: TaskPose, tasks: CompanionTaskGroup['tasks'], poses: TaskPose[]) {
  const task = tasks.find(task => task.id === card.id)
  if (task?.parentTaskId) return poses.some(pose => pose.id === task.parentTaskId && !pose.hidden && !pose.depth)
    ? { kind: 'cards' as const, id: task.parentTaskId } : null
  return { kind: 'projects' as const, id: card.projectId }
}

/** Regroup using the existing automatic shape, with other saved nodes fixed.
 * No available gap means no move; gathering must not cover a different project. */
export function companionGatherPlacements(base: OverviewGeometry, overview: OverviewGeometry, desktop: CompanionDesktop, id: string,
  character: { x: number; y: number; width: number; height: number }) {
  const badge = overview.projects.find(node => node.id === id), source = base.projects.find(node => node.id === id)
  if (!badge || !source) return null
  const area = localWorkArea(desktop.displays.find(display => display.id === nodeDisplay(badge, desktop))!, desktop)
  const factor = badge.scale / source.scale
  const nodes = [...base.projects.filter(node => node.id === id).map(node => ({ ...node, kind: 'projects' as const })),
    ...base.cards.filter(node => node.projectId === id).map(node => ({ ...node, kind: 'tasks' as const }))]
  const visible = nodes.filter(node => !('hidden' in node) || !node.hidden)
  const left = Math.min(...visible.map(node => node.x)), top = Math.min(...visible.map(node => node.y))
  const boxes = visible.map(node => ({ x: (node.x - left) * factor, y: (node.y - top) * factor,
    width: node.width * node.scale * factor, height: node.height * node.scale * factor }))
  const obstacles = [character, ...[...overview.projects.filter(node => node.id !== id),
    ...overview.cards.filter(node => node.projectId !== id && !node.hidden)]
    .map(node => ({ x: node.x + 24, y: node.y + 28, width: node.width * node.scale, height: node.height * node.scale }))]
  const width = Math.max(...boxes.map(box => box.x + box.width)), height = Math.max(...boxes.map(box => box.y + box.height))
  const candidates: { x: number; y: number; cost: number }[] = []
  for (let y = area.y + 16; y <= area.y + area.height - height - 16; y += 20)
    for (let x = area.x + 16; x <= area.x + area.width - width - 16; x += 20) {
      if (boxes.some(box => obstacles.some(other => x + box.x < other.x + other.width + 12 && x + box.x + box.width + 12 > other.x
        && y + box.y < other.y + other.height + 12 && y + box.y + box.height + 12 > other.y))) continue
      candidates.push({ x, y, cost: Math.hypot(x + (source.x - left) * factor - badge.x - 24, y + (source.y - top) * factor - badge.y - 28) })
    }
  const destination = candidates.sort((a,b) => a.cost - b.cost)[0]
  if (!destination) return null
  return new Map(nodes.map(node => [`${node.kind}:${node.id}`, { x: destination.x + (node.x - left) * factor,
    y: destination.y + (node.y - top) * factor, scale: node.scale * factor }]))
}

export function selectDisplayTask(focuses: DisplayFocuses, displayId: number, selection: Selection, parentTaskId?: string): DisplayFocuses {
  const before = focuses[displayId] || { trail: [] }, trail = before.trail, latest = trail.at(-1)
  const next = !selection.taskId ? [selection]
    : latest?.projectId === selection.projectId && !latest.taskId ? [...trail, selection]
    : latest?.taskId && parentTaskId === latest.taskId ? [...trail, selection]
    : trail.length > 1 && trail.at(-2)?.taskId === selection.taskId ? trail.slice(0,-1)
    : latest?.taskId ? [...trail.slice(0,-1), selection] : [selection]
  return { ...focuses, [displayId]: { trail: next } }
}
export function backDisplay(focuses: DisplayFocuses, displayId: number): DisplayFocuses {
  const before = focuses[displayId]
  if (!before) return focuses
  const next = { ...focuses }
  if (before.trail.length > 1) next[displayId] = { ...before, trail: before.trail.slice(0,-1) }
  else delete next[displayId]
  return next
}

/** Project identity does not grant a reader authority to reposition cards on
 * another display, including cards belonging to the very same project. */
export function displayFocusLayout(overview: OverviewGeometry, groups: CompanionTaskGroup[], desktop: CompanionDesktop,
  displayId: number, focus: DisplayFocus, mode: LayoutMode, focusHeight: number, readingBottom: number, character?: Box | null) {
  const display = desktop.displays.find(display => display.id === displayId)
  const selection = focus.trail.at(-1), group = groups.find(group => group.id === selection?.projectId)
  if (!display || !selection || !group) return null
  const task = group.tasks.find(task => task.id === selection.taskId)
  if (selection.taskId && !task) return null
  const projectIds = new Set(overview.projects.filter(pose => nodeDisplay(pose, desktop) === displayId).map(pose => pose.id))
  const taskIds = new Set(overview.cards.filter(pose => nodeDisplay(pose, desktop) === displayId).map(pose => pose.id))
  // A child which has not yet appeared in the overview can be read alongside
  // its parent. Existing cards on another display are routed to that display.
  if (task && !overview.cards.some(pose => pose.id === task.id)) taskIds.add(task.id)
  const localGroups = groups.map(group => ({ ...group, tasks: group.tasks.filter(task => taskIds.has(task.id)) }))
    .filter(group => group.tasks.length || projectIds.has(group.id))
  const area = localWorkArea(display, desktop)
  const geometry = constellationLayout(localGroups, area.width - 48, area.height - 52, readingBottom,
    group.id, task?.id || '', focusHeight, [], { mode, readingBottom,
      character: character ? { ...character, x: character.x - area.x - 24, y: character.y - area.y - 28 } : character,
      reserveRail: [...taskIds].some(id => id !== task?.id) || projectIds.size > 0 })
  const projects = geometry.projects.filter(pose => projectIds.has(pose.id)).map(pose => ({ ...pose,
    count: overview.projects.find(project => project.id === pose.id)!.count, x: pose.x + area.x, y: pose.y + area.y }))
  const cards = geometry.cards.map(pose => ({ ...pose, x: pose.x + area.x, y: pose.y + area.y }))
  return { displayId, area, group, task, focus, geometry, projects, cards, projectIds, taskIds }
}

export function composeDisplayFocus(overview: OverviewGeometry, views: NonNullable<ReturnType<typeof displayFocusLayout>>[]) {
  const projects = new Map(overview.projects.map(pose => [pose.id, pose]))
  const cards = new Map(overview.cards.map(pose => [pose.id, pose]))
  for (const view of views) {
    for (const pose of view.projects) projects.set(pose.id, pose)
    for (const pose of view.cards) cards.set(pose.id, pose)
  }
  return { ...overview, projects: [...projects.values()], cards: [...cards.values()] }
}
