import type { CompanionTaskGroup } from './floatingCompanionState'

export type NodePose = { x: number; y: number; width: number; height: number; scale: number }
export type TaskPose = NodePose & { id: string; projectId: string; depth: number; hidden: boolean; mini: boolean; focused: boolean }
export type ProjectPose = NodePose & { id: string; stacked: boolean; count: number; frontId: string; mini: boolean }
/** Project footprints, independent of task wording, status and reading navigation. */
export type OverviewSlot = {
  id: string; count: number; x: number; y: number; width: number; height: number
  cardWidth: number; capacity: number; stagger: number
}
type Size = { id: string; count: number }
const hash = (text: string) => [...text].reduce((value, char) => Math.imul(value ^ char.charCodeAt(0), 16777619) >>> 0, 2166136261)
const intersects = (a: OverviewSlot, b: OverviewSlot, gap = 24) =>
  a.x < b.x + b.width + gap && b.x < a.x + a.width + gap && a.y < b.y + b.height + gap && b.y < a.y + a.height + gap

/** Bounded footprint packing. Existing anchors win when still feasible; identity
 * only supplies a spatial preference, never a prescribed slot or reshuffle timer. */
export function overviewLayout(items: Size[], width: number, height: number, characterTop: number,
  previous: OverviewSlot[] = []): OverviewSlot[] {
  if (!items.length) return []
  const cardWidth = Math.min(300, (width - 64) / 3)
  const sideWidth = Math.min(232, width * .23)
  const prior = new Map(previous.map(slot => [slot.id, slot]))
  const retained = items.map(item => prior.get(item.id))
  if (retained.every((slot, i) => slot && slot.count === items[i].count && slot.x + slot.width <= width - 8
    && slot.y + slot.height <= height - 30 && (slot.y + slot.height <= characterTop - 16
      || slot.x + slot.width <= width * .26 || slot.x >= width * .74))) return retained as OverviewSlot[]
  const ordered = [...items].sort((a, b) => Number(prior.has(b.id)) - Number(prior.has(a.id)) || b.count - a.count || hash(a.id) - hash(b.id))
  type Plan = { slots: OverviewSlot[]; cost: number }
  const balance = (slots: OverviewSlot[]) => {
    const area = slots.reduce((sum, slot) => sum + slot.width * slot.height, 0)
    const centre = slots.reduce((sum, slot) => sum + (slot.x + slot.width / 2) * slot.width * slot.height, 0) / (area || 1)
    return slots.length ? Math.abs(centre - width / 2) / width * 1.1 : 0
  }
  let plans: Plan[] = [{ slots: [], cost: 0 }]
  for (const item of ordered) {
    const seed = hash(item.id), old = prior.get(item.id)
    const stagger = 12 + seed % 19
    const shapes = [
      ...(item.count === 2 ? [{ width: cardWidth * 2 + 26, height: 246 + stagger, cardWidth, capacity: 2, stagger }] : []),
      { width: cardWidth + (item.count > 1 ? 14 : 0), height: item.count > 1 ? 264 : 246, cardWidth, capacity: 1, stagger: 0 },
      { width: sideWidth + (item.count > 1 ? 14 : 0), height: item.count > 1 ? 264 : 246, cardWidth: sideWidth, capacity: 1, stagger: 0 },
    ]
    const candidates: { slot: OverviewSlot; cost: number }[] = []
    for (const shape of shapes) {
      const maxX = width - shape.width - 12
      const maxY = Math.min(height - shape.height - 32, characterTop + 500)
      const targetX = 20 + (maxX - 20) * ((seed % 997) / 997)
      const targetY = 36 + Math.max(0, characterTop - shape.height - 90) * (((seed >>> 10) % 991) / 991)
      const positions = [{ x: targetX, y: targetY }, ...(old ? [{ x: old.x, y: old.y }] : [])]
      for (let y = 24 + seed % 23; y <= maxY; y += 34)
        for (let x = 12 + (seed >>> 8) % 19; x <= maxX; x += 34) positions.push({ x, y })
      // Include both narrow side corridors even when the sample grid misses their edge.
      for (let y = characterTop + 36 + seed % 31; y <= maxY; y += 48)
        positions.push({ x: 12, y }, { x: maxX, y })
      for (const point of positions) {
        const slot = { ...item, ...shape, ...point }
        if (slot.x < 8 || slot.y < 16 || slot.x + slot.width > width - 8 || slot.y + slot.height > height - 30) continue
        if (slot.y + slot.height > characterTop - 16 && slot.x < width * .74 && slot.x + slot.width > width * .26) continue
        const distance = Math.hypot(slot.x - targetX, slot.y - targetY) / width
        const movement = old ? Math.hypot(slot.x - old.x, slot.y - old.y) / width : 0
        const sameSize = old?.count === item.count
        const shapeChange = old && sameSize && (old.capacity !== shape.capacity || old.cardWidth !== shape.cardWidth) ? .6 : 0
        const cost = distance + movement * (sameSize ? 9 : 2) + shapeChange + (slot.y > characterTop ? .45 : 0)
          + (shape.cardWidth < cardWidth ? .65 : 0) + (item.count === 2 && shape.capacity === 1 ? 2 : 0)
        candidates.push({ slot, cost })
      }
    }
    const next: Plan[] = []
    for (const plan of plans) {
      const fits = candidates.filter(candidate => !plan.slots.some(slot => intersects(slot, candidate.slot)))
        .map(candidate => ({ ...candidate, cost: candidate.cost + balance([...plan.slots, candidate.slot]) - balance(plan.slots) }))
        .sort((a, b) => a.cost - b.cost)
      // Keep alternatives separated spatially so the beam can escape a crowded region.
      const alternatives: typeof fits = []
      for (const candidate of fits) {
        if (alternatives.some(other => other.slot.capacity === candidate.slot.capacity && Math.hypot(other.slot.x - candidate.slot.x, other.slot.y - candidate.slot.y) < 90)) continue
        alternatives.push(candidate)
        next.push({ slots: [...plan.slots, candidate.slot], cost: plan.cost + candidate.cost })
        if (alternatives.length === 10) break
      }
    }
    plans = next.sort((a, b) => a.cost - b.cost).slice(0, 18)
    // An undersized surface uses the existing readable, scrollable fallback.
    if (!plans.length) return []
  }
  return items.map(item => plans[0].slots.find(slot => slot.id === item.id)!)
}

const CARD_HEIGHT = 184
const rank = { attention: 0, blocked: 1, running: 2, ready: 3, idle: 4 }
export const rootTasks = (group: CompanionTaskGroup) => group.tasks.filter(task => !group.tasks.some(parent => parent.id === task.parentTaskId))

/** One pose per real task. The selected card moves to reading; it has no rail copy. */
export function constellationLayout(groups: CompanionTaskGroup[], width: number, height: number, characterTop: number,
  projectId = '', taskId = '', taskPage = 0, focusHeight = 112, previous: OverviewSlot[] = []) {
  let compact = width < 700 || height < 900
  const top = Math.max(300, characterTop - 24)
  const cardWidth = Math.min(300, (width - 64) / (compact ? 1 : 3))
  const railWidth = Math.min(244, width * .29)
  const readingWidth = Math.min(660, width - railWidth - 46)
  const projects: ProjectPose[] = [], cards: TaskPose[] = []
  let railY = 78, pages = 1, activePage = 0
  let slots = compact ? [] : overviewLayout(groups.map(group => ({ id: group.id, count: rootTasks(group).length })), width, height, characterTop, previous)
  if (groups.length && !slots.length) {
    compact = true
    slots = groups.map(group => ({ id: group.id, count: rootTasks(group).length,
      x: 12, y: 32, width: cardWidth, height: 264, cardWidth, capacity: 1, stagger: 0 }))
  }
  groups.forEach((group, groupIndex) => {
    const roots = rootTasks(group)
    const ordered = [...roots].sort((a, b) => rank[a.phase] - rank[b.phase])
    const slot = slots[groupIndex % 5]
    const selectedProject = group.id === projectId
    const mini = Boolean(projectId && (taskId || !selectedProject))
    const stacked = !projectId && roots.length > (compact ? 1 : slot.capacity)
    const selectedChild = group.tasks.find(task => task.id === taskId && task.parentTaskId)
    const visibleRoots = stacked ? ordered : mini && selectedChild
      ? [...roots].sort((a, b) => Number(b.id === selectedChild.parentTaskId) - Number(a.id === selectedChild.parentTaskId)) : roots
    let badge: NodePose
    if (mini) {
      badge = { x: width - railWidth - 8, y: railY, width: railWidth, height: 34, scale: 1 }
    } else if (selectedProject) {
      badge = { x: 18, y: 66, width: Math.min(300, readingWidth), height: 36, scale: 1 }
    } else {
      const badgeWidth = Math.min(slot.width * .92, 280)
      badge = { x: slot.x + (slot.width - badgeWidth) / 2, y: slot.y, width: badgeWidth, height: 36, scale: 1 }
    }
    projects.push({ ...badge, id: group.id, count: roots.length, frontId: visibleRoots[0]?.id || '', stacked, mini })
    const columns = Math.max(1, Math.floor((readingWidth + 20) / (cardWidth + 20)))
    const capacity = Math.max(1, Math.floor((top - 130) / (CARD_HEIGHT + 24))) * columns
    if (selectedProject && !taskId) {
      pages = Math.max(1, Math.ceil(roots.length / capacity))
      activePage = Math.min(Math.max(0, taskPage), pages - 1)
    }
    let miniIndex = 0
    for (const [i, task] of visibleRoots.entries()) {
      let pose: TaskPose = { id: task.id, projectId: group.id, x: 0, y: 0, width: cardWidth, height: CARD_HEIGHT,
        scale: 1, depth: 0, hidden: false, mini, focused: task.id === taskId }
      if (task.id === taskId) {
        pose = { ...pose, x: 8, y: 66, width: readingWidth, height: focusHeight, mini: false }
      } else if (mini) {
        const scale = (railWidth - 20) / (2 * cardWidth)
        const position = Math.min(miniIndex, 3)
        pose = { ...pose, x: badge.x + (position % 2) * (cardWidth * scale + 12), y: badge.y + 52 + Math.floor(position / 2) * (CARD_HEIGHT * scale + 14), scale, hidden: miniIndex >= 4 }
        miniIndex++
      } else if (selectedProject) {
        const local = i - activePage * capacity
        const position = Math.min(capacity - 1, Math.max(0, local))
        pose = { ...pose, x: 12 + (position % columns) * (cardWidth + 24),
          y: 128 + Math.floor(position / columns) * (CARD_HEIGHT + 24) + (position % 2 ? 10 : 0), hidden: local < 0 || local >= capacity }
      } else if (stacked) {
        const depth = Math.min(i, 2)
        pose = { ...pose, width: slot.cardWidth, x: slot.x + depth * 7, y: slot.y + 62 + depth * 9,
          depth, hidden: i > 2, scale: 1 - depth * .025 }
      } else {
        const sideBySide = slot.capacity === 2
        pose = { ...pose, width: slot.cardWidth,
          x: slot.x + (sideBySide ? i * (slot.cardWidth + 26) : i * 13),
          y: slot.y + 62 + (sideBySide ? i * slot.stagger : i * (CARD_HEIGHT + 24)) }
      }
      cards.push(pose)
    }
    // Child agents enter the graph only when selected and connect to their parent.
    const child = group.tasks.find(task => task.id === taskId && !roots.some(root => root.id === task.id))
    if (child) cards.push({ id: child.id, projectId: group.id, x: 8, y: 66, width: readingWidth, height: focusHeight,
      scale: 1, depth: 0, hidden: false, mini: false, focused: true })
    if (mini) railY += 52 + Math.ceil(Math.min(miniIndex, 4) / 2) * (CARD_HEIGHT * (railWidth - 20) / (2 * cardWidth) + 14) + 28
  })
  // Small primary fallback prioritises access. Five projects without scrolling is
  // guaranteed on the full portrait secondary display, not a 420px overlay.
  if (compact && !projectId) projects.forEach((project, i) => {
    project.x = 12; project.y = 32 + i * 258; project.width = Math.min(width - 30, 280)
    const owned = cards.filter(card => card.projectId === project.id)
    project.stacked = owned.length > 1
    owned.forEach((card, index) => Object.assign(card, { x: 12 + Math.min(index, 2) * 6, y: project.y + 54 + Math.min(index, 2) * 8,
      width: Math.min(300, width - 42), depth: Math.min(index, 2), hidden: index > 2 }))
  })
  const bottom = Math.max(height, ...cards.filter(card => !card.hidden).map(card => card.y + card.height * card.scale + 30))
  const readingTop = 66 + focusHeight + 16
  return { slots, projects, cards, readingWidth, readingTop, readingHeight: Math.max(200, characterTop - readingTop - 8),
    height: bottom, taskPages: pages, taskPage: activePage, compact }
}
