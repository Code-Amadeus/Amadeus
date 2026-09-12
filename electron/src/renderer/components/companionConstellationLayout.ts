import type { CompanionTaskGroup } from './floatingCompanionState'
import type { LayoutMode } from './companionLayoutPreferences'
import { projectOverviewMap, projectMiniature, type MapBox } from './companionProjectMap.ts'
import { stackMetrics, STACK_LAYER_LIMIT, STACK_STEP } from './companionStack.ts'

export type NodePose = { x: number; y: number; width: number; height: number; scale: number }
export type TaskPose = NodePose & { id: string; projectId: string; depth: number; hidden: boolean; mini: boolean; focused: boolean; compactParent?: boolean; stackId?: string; projectOverview?: boolean }
export type ProjectPose = NodePose & { id: string; stacked: boolean; count: number; mini: boolean }
/** Project footprints, independent of task wording, status and reading navigation. */
export type OverviewSlot = {
  id: string; count: number; x: number; y: number; width: number; height: number
  cardWidth: number; capacity: number; stagger: number
  signature?: string; nodes?: FamilyNode[]
}
type Member = { id: string; parentTaskId?: string; contextOnly?: boolean }
type FamilyNode = { id: string; x: number; y: number; depth: number; hidden: boolean; compactParent?: boolean; stackId?: string }
type Size = { id: string; count: number; members: Member[] }
export type LayoutOptions = {
  mode?: LayoutMode; seed?: number; pinned?: Set<string>
  readingBottom?: number
  character?: MapBox | null
  reserveRail?: boolean
  occupied?: (slot: OverviewSlot) => { x: number; y: number; width: number; height: number }
}
const CARD_HEIGHT = 184
const memberHeight = (member: Member) => member.contextOnly ? 42 : CARD_HEIGHT
const foldedHeight = (member: Member, node: FamilyNode) => member.contextOnly ? 42 : node.compactParent ? 58 : CARD_HEIGHT
const hash = (text: string) => [...text].reduce((value, char) => Math.imul(value ^ char.charCodeAt(0), 16777619) >>> 0, 2166136261)
const intersects = (a: { x: number; y: number; width: number; height: number }, b: typeof a, gap = 24) =>
  a.x < b.x + b.width + gap && b.x < a.x + a.width + gap && a.y < b.y + b.height + gap && b.y < a.y + a.height + gap

/** Collapse siblings only. A parent remains a visible endpoint even when it
 * has an active result; presentation compactness never changes task identity. */
function foldedFamilyShape(members: Member[], cardWidth: number) {
  const nodes: FamilyNode[] = [], visited = new Set<string>()
  const childrenOf = (id: string) => members.filter(member => member.parentTaskId === id && member.id !== id)
  const place = (siblings: Member[], x: number, y: number): number => {
    const branches = siblings.filter(member => childrenOf(member.id).length)
    const leaves = siblings.filter(member => !childrenOf(member.id).length)
    for (const branch of branches) {
      if (visited.has(branch.id)) continue
      visited.add(branch.id)
      const node = { id: branch.id, x, y, depth: 0, hidden: false, compactParent: true }
      nodes.push(node)
      y = place(childrenOf(branch.id), x + 18, y + foldedHeight(branch, node) + 24) + 30
    }
    const pile = leaves.filter(member => !visited.has(member.id))
    const stackId = pile.length > 1 ? `siblings:${pile.map(member => member.id).sort()[0]}` : undefined
    pile.forEach((member, depth) => {
      visited.add(member.id)
      const layer = Math.min(depth, STACK_LAYER_LIMIT - 1)
      nodes.push({ id: member.id, x: x + layer * 3, y: y + layer * STACK_STEP, depth,
        hidden: depth >= STACK_LAYER_LIMIT, stackId })
    })
    return pile.length ? y + Math.max(...pile.map(memberHeight)) + stackMetrics(pile.length).tail : Math.max(0, y - 30)
  }
  const roots = members.filter(member => !members.some(parent => parent.id === member.parentTaskId))
  place(roots, 0, 0)
  const width = Math.max(cardWidth, ...nodes.map(node => node.x + cardWidth))
  const height = Math.max(0, ...nodes.filter(node => !node.hidden).map(node => node.y + foldedHeight(members.find(member => member.id === node.id)!, node))) + 62
  return { width, height, cardWidth, capacity: 1, stagger: 0, nodes }
}

/** A family's footprint is reserved BEFORE project packing. Columns contain
 * complete families, never the next item in a flattened preorder. */
export function familyShape(members: Member[], cardWidth: number, mode: LayoutMode = 'natural', collapsed = false, columns = 2) {
  if (collapsed) return foldedFamilyShape(members, cardWidth)
  const roots = members.filter(member => !members.some(parent => parent.id === member.parentTaskId))
  const nodes: FamilyNode[] = []
  let rowX = 0, rowY = 0, rowHeight = 0, totalWidth = 0
  roots.forEach((root, index) => {
    const family: FamilyNode[] = []
    const visit = (member: Member, x: number, y: number) => {
      const children = members.filter(child => child.parentTaskId === member.id)
      family.push({ id: member.id, x, y, depth: 0, hidden: false })
      let bottom = y + memberHeight(member)
      children.forEach(child => {
        if (family.some(item => item.id === child.id)) return
        const shift = mode === 'ordered' ? 20 : Math.round(cardWidth * (.14 + hash(child.id) % 8 / 100))
        const childBottom = visit(child, x + shift, bottom + 24)
        bottom = Math.max(bottom, childBottom)
      })
      return bottom
    }
    visit(root, 0, 0)
    // Mirror the whole family, keeping siblings and grandchildren in its lane.
    const familyWidth = Math.max(...family.map(node => node.x + cardWidth))
    if (mode !== 'ordered' && hash(root.id) % 2) for (const node of family) node.x = familyWidth - cardWidth - node.x
    const familyHeight = Math.max(...family.filter(node => !node.hidden).map(node => node.y + memberHeight(members.find(member => member.id === node.id)!)))
    if (index % columns === 0 && index > 0) { rowX = 0; rowY += rowHeight + 38; rowHeight = 0 }
    const stagger = mode === 'ordered' || index % columns === 0 ? 0 : 12 + hash(root.id) % 19
    for (const node of family) nodes.push({ ...node, x: rowX + node.x, y: rowY + stagger + node.y })
    rowHeight = Math.max(rowHeight, familyHeight + stagger)
    totalWidth = Math.max(totalWidth, rowX + familyWidth)
    rowX += familyWidth + 38
  })
  return { width: totalWidth, height: rowY + rowHeight + 62, cardWidth, capacity: members.length, stagger: 0, nodes }
}

/** Bounded footprint packing. Existing anchors win when still feasible; identity
 * only supplies a spatial preference, never a prescribed slot or reshuffle timer. */
export function overviewLayout(items: Size[], width: number, height: number, characterTop: number,
  previous: OverviewSlot[] = [], options: LayoutOptions = {}): OverviewSlot[] {
  if (!items.length) return []
  const cardWidth = Math.min(300, (width - 64) / 3)
  const sideWidth = Math.min(232, width * .23)
  const prior = new Map(previous.map(slot => [slot.id, slot]))
  const mode = options.mode || 'natural'
  const occupied = options.occupied || ((slot: OverviewSlot) => slot)
  const signature = (item: Size) => 'siblings-v2:' + item.members.map(m => `${m.id}:${m.parentTaskId || ''}:${Boolean(m.contextOnly)}`).sort().join('|')
  const retained = items.map(item => prior.get(item.id))
  if (retained.every((slot, i) => slot && slot.signature === signature(items[i]) && slot.count === items[i].count && slot.x + slot.width <= width - 8
    && slot.y + slot.height <= height - 30 && (slot.y + slot.height <= characterTop - 16
      || slot.x + slot.width <= width * .26 || slot.x >= width * .74))) return retained as OverviewSlot[]
  const ordered = [...items].sort((a, b) => Number(options.pinned?.has(b.id)) - Number(options.pinned?.has(a.id)) || Number(prior.has(b.id)) - Number(prior.has(a.id)) || (mode === 'ordered' ? a.id.localeCompare(b.id) : b.count - a.count || hash(a.id) - hash(b.id)))
  type Plan = { slots: OverviewSlot[]; cost: number }
  const balance = (slots: OverviewSlot[]) => {
    const area = slots.reduce((sum, slot) => sum + slot.width * slot.height, 0)
    const centre = slots.reduce((sum, slot) => sum + (slot.x + slot.width / 2) * slot.width * slot.height, 0) / (area || 1)
    return slots.length ? Math.abs(centre - width / 2) / width * 1.1 : 0
  }
  let plans: Plan[] = [{ slots: [], cost: 0 }]
  for (const item of ordered) {
    const seed = hash(item.id + (mode === 'scattered' ? `:${options.seed || 0}` : '')), old = prior.get(item.id)
    const full = familyShape(item.members, cardWidth, mode)
    const folded = familyShape(item.members, cardWidth, mode, true)
    const shapes = [
      ...(item.count <= 6 ? [full] : []),
      folded,
      familyShape(item.members, sideWidth, mode, true),
    ]
    const candidates: { slot: OverviewSlot; cost: number }[] = []
    for (const shape of shapes) {
      const maxX = width - shape.width - 12
      const maxY = Math.min(height - shape.height - 32, characterTop + 500)
      const targetX = 20 + (maxX - 20) * ((seed % 997) / 997)
      const targetY = 36 + Math.max(0, (mode === 'scattered' ? height - 120 : characterTop - 90) - shape.height) * (((seed >>> 10) % 991) / 991)
      const positions = [{ x: targetX, y: targetY }, ...(old ? [{ x: old.x, y: old.y }] : [])]
      for (let y = 24 + (mode === 'ordered' ? 0 : seed % 23); y <= maxY; y += mode === 'ordered' ? 288 : 34)
        for (let x = 12 + (mode === 'ordered' ? 0 : (seed >>> 8) % 19); x <= maxX; x += mode === 'ordered' ? cardWidth + 38 : 34) positions.push({ x, y })
      // Include both narrow side corridors even when the sample grid misses their edge.
      for (let y = characterTop + 36 + seed % 31; y <= maxY; y += 48)
        positions.push({ x: 12, y: mode === 'ordered' ? Math.ceil(y / 288) * 288 + 24 : y }, { x: maxX, y: mode === 'ordered' ? Math.ceil(y / 288) * 288 + 24 : y })
      for (const point of positions) {
        if (mode === 'ordered' && point === positions[0]) continue
        const slot: OverviewSlot = { id: item.id, count: item.count, signature: signature(item), ...shape, ...point }
        if (old && options.pinned?.has(item.id) && (slot.x !== old.x || slot.y !== old.y)) continue
        if (slot.x < 8 || slot.y < 16 || slot.x + slot.width > width - 8 || slot.y + slot.height > height - 30) continue
        if (slot.y + slot.height > characterTop - 16 && slot.x < width * .74 && slot.x + slot.width > width * .26) continue
        const distance = mode === 'ordered' ? slot.y / height * 2 + slot.x / width * .15 : Math.hypot(slot.x - targetX, slot.y - targetY) / width
        const movement = old ? Math.hypot(slot.x - old.x, slot.y - old.y) / width : 0
        const sameSize = old?.count === item.count
        const shapeChange = old && sameSize && (old.capacity !== shape.capacity || old.cardWidth !== shape.cardWidth) ? .6 : 0
        const cost = distance + movement * (sameSize ? 9 : 2) + shapeChange + (slot.y > characterTop ? .45 : 0)
          + (shape.cardWidth < cardWidth ? .65 : 0) + (item.count > 1 && item.count <= 4 && shape.capacity === 1 ? 2 : 0)
        candidates.push({ slot, cost })
      }
    }
    const next: Plan[] = []
    for (const plan of plans) {
      const fits = candidates.filter(candidate => !plan.slots.some(slot => intersects(occupied(slot), occupied(candidate.slot))))
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

export const rootTasks = (group: CompanionTaskGroup) => group.tasks.filter(task => !group.tasks.some(parent => parent.id === task.parentTaskId))

/** A root below another family must not borrow that family's visual trunk.
 * Route around intervening cards instead of drawing a hidden project edge. */
export function projectBranchPath(a: { left: number; top: number; right: number; bottom: number }, b: typeof a, obstacles: typeof a[]) {
  const x = (a.left + a.right) / 2, y = a.bottom, tx = b.left + (b.right - b.left) * .45, ty = b.top
  let blocked = false, left = Math.min(a.left, b.left) - 16, right = Math.max(a.right, b.right) + 16
  for (const box of obstacles) {
    if (box === b || box.top >= Math.max(y, ty) || box.bottom <= Math.min(y, ty)
      || box.left >= Math.max(x, tx) + 12 || box.right <= Math.min(x, tx) - 12) continue
    blocked = true; left = Math.min(left, box.left - 16); right = Math.max(right, box.right + 16)
  }
  if (!blocked) return `M ${x} ${y} C ${x} ${y + 22}, ${tx} ${ty - 28}, ${tx} ${ty}`
  const useLeft = x + tx - left * 2 <= right * 2 - x - tx
  const gutter = useLeft ? left : right, from = useLeft ? a.left : a.right, to = useLeft ? b.left : b.right
  const ay = (a.top + a.bottom) / 2, by = (b.top + b.bottom) / 2
  const radius = Math.min(12, Math.abs(by - ay) / 3), sign = Math.sign(by - ay)
  return `M ${from} ${ay} C ${gutter} ${ay}, ${gutter} ${ay}, ${gutter} ${ay + radius * sign} L ${gutter} ${by - radius * sign} C ${gutter} ${by}, ${gutter} ${by}, ${to} ${by}`
}

/** Use facing card edges so a branch connection stays in the visible gap. */
export function taskBranchPath(a: { left: number; top: number; right: number; bottom: number },
  b: { left: number; top: number; right: number; bottom: number }, obstacles: typeof a[] = []) {
  if (b.left >= a.right || b.right <= a.left) {
    const x = b.left >= a.right ? a.right : a.left, tx = b.left >= a.right ? b.left : b.right
    const y = (a.top + a.bottom) / 2, ty = (b.top + b.bottom) / 2, bend = (tx - x) * .5
    return `M ${x} ${y} C ${x + bend} ${y}, ${tx - bend} ${ty}, ${tx} ${ty}`
  }
  if (b.top >= a.bottom || b.bottom <= a.top) {
    const y = b.top >= a.bottom ? a.bottom : a.top, ty = b.top >= a.bottom ? b.top : b.bottom
    const x = (a.left + a.right) / 2, tx = (b.left + b.right) / 2, bend = (ty - y) * .5
    let blocked = false, gutter = Math.max(a.right, b.right) + 12
    for (const box of obstacles) if (box !== a && box !== b && box.top < Math.max(y, ty) && box.bottom > Math.min(y, ty)
      && box.left < Math.max(x, tx) && box.right > Math.min(x, tx)) {
      blocked = true; gutter = Math.max(gutter, box.right + 12)
    }
    if (blocked) {
      // Siblings share a parent, not a chain through the sibling in between.
      // Use the family's right gutter, separate from the project trunk.
      const ay = (a.top + a.bottom) / 2, by = (b.top + b.bottom) / 2, sign = Math.sign(by - ay)
      const radius = Math.min(10, Math.abs(by - ay) / 3)
      return `M ${a.right} ${ay} C ${gutter} ${ay}, ${gutter} ${ay}, ${gutter} ${ay + radius * sign} L ${gutter} ${by - radius * sign} C ${gutter} ${by}, ${gutter} ${by}, ${b.right} ${by}`
    }
    return `M ${x} ${y} C ${x} ${y + bend}, ${tx} ${ty - bend}, ${tx} ${ty}`
  }
  return '' // Cards can briefly overlap while moving; do not draw through them.
}

/** Cross-monitor links end at each work-area edge rather than painting a long
 * line through unrelated windows. The parent identity remains on the reader. */
export function crossDisplayBranchPath(a: { left: number; top: number; right: number; bottom: number }, b: typeof a,
  from: { x: number; y: number; width: number; height: number }, to: typeof from) {
  const dx = to.x + to.width / 2 - from.x - from.width / 2
  const dy = to.y + to.height / 2 - from.y - from.height / 2
  const horizontal = Math.abs(dx) >= Math.abs(dy)
  const end = (box: typeof a, area: typeof from, direction: number) => {
    if (horizontal) {
      const y = Math.max(area.y + 20, Math.min(area.y + area.height - 20, (box.top + box.bottom) / 2))
      return { x: direction > 0 ? area.x + area.width - 6 : area.x + 6, y }
    }
    const x = Math.max(area.x + 20, Math.min(area.x + area.width - 20, (box.left + box.right) / 2))
    return { x, y: direction > 0 ? area.y + area.height - 6 : area.y + 6 }
  }
  const direction = Math.sign(horizontal ? dx : dy) || 1, exit = end(a, from, direction), entry = end(b, to, -direction)
  const dot = (point: typeof exit) => ({ left: point.x, right: point.x + 1, top: point.y, bottom: point.y + 1 })
  return `${taskBranchPath(a, dot(exit))} ${taskBranchPath(dot(entry), b)}`.trim()
}

/** Side conversations are user-visible task branches, unlike worker agents.
 * Keep each family adjacent and retain the real parent as its connection. */
export function familyTasks(group: CompanionTaskGroup) {
  const ordered: CompanionTaskGroup['tasks'] = []
  const visit = (task: CompanionTaskGroup['tasks'][number]) => {
    if (ordered.some(item => item.id === task.id)) return
    ordered.push(task)
    group.tasks.filter(child => child.sourceKind === 'sidechat' && child.parentTaskId === task.id).forEach(visit)
  }
  rootTasks(group).forEach(visit)
  return ordered
}

/** One pose per real task. The selected card moves to reading; it has no rail copy. */
export function constellationLayout(groups: CompanionTaskGroup[], width: number, height: number, characterTop: number,
  projectId = '', taskId = '', focusHeight = 112, previous: OverviewSlot[] = [], options: LayoutOptions = {}) {
  let compact = width < 700 || height < 900
  const cardWidth = Math.min(300, (width - 64) / (compact ? 1 : 3))
  const railWidth = options.reserveRail === false ? 0 : Math.min(156, width * .19)
  const readingWidth = Math.min(760, width - railWidth - 46)
  const projects: ProjectPose[] = [], cards: TaskPose[] = []
  let railY = 78
  const railGroups = groups.filter(group => projectId && (taskId || group.id !== projectId))
  const railBudget = Math.max(40, (height - 120) / Math.max(1, railGroups.length) - 80)
  const miniatures = new Map(railGroups.map(group => {
    const shape = projectMiniature(familyTasks(group).filter(task => task.id !== taskId), Math.max(50, railWidth - 16))
    const scale = Math.min(1, railBudget / Math.max(1, shape.height))
    const result = { ...shape, scale, y: railY }; railY += 80 + shape.height * scale
    return [group.id, result]
  }))
  const character = options.character === undefined
    ? { x: width * .25, y: characterTop, width: width * .5, height: height - characterTop } : options.character
  const selected = groups.find(group => group.id === projectId)
  const spread = selected && !taskId ? projectOverviewMap(familyTasks(selected), width, height,
    railGroups.length && railWidth ? [{ x: width - railWidth - 8, y: 66, width: railWidth + 8, height: railY - 66 }] : [], character || undefined) : null
  const spreadNodes = new Map(spread?.nodes.map(node => [node.id, node]))
  let slots = compact ? [] : overviewLayout(groups.map(group => ({ id: group.id, count: familyTasks(group).length,
    members: familyTasks(group).map(task => ({ id: task.id, parentTaskId: task.parentTaskId, contextOnly: task.contextOnly })) })), width, height, characterTop, previous, options)
  if (groups.length && !slots.length) {
    compact = true
    slots = groups.map(group => ({ id: group.id, count: familyTasks(group).length,
      x: 12, y: 32, ...familyShape(familyTasks(group), Math.min(cardWidth, width - 72), options.mode, true) }))
  }
  groups.forEach((group, groupIndex) => {
    const family = familyTasks(group)
    const slot = slots[groupIndex % 5]
    const selectedProject = group.id === projectId
    const mini = Boolean(projectId && (taskId || !selectedProject))
    const stacked = !projectId && Boolean(slot.nodes?.some(node => node.stackId))
    const miniature = miniatures.get(group.id)
    let badge: NodePose
    if (mini) {
      badge = { x: width - railWidth - 8, y: miniature!.y, width: railWidth, height: 52, scale: 1 }
    } else if (selectedProject) {
      badge = { x: 18, y: 66, width: Math.min(300, readingWidth), height: 36, scale: 1 }
    } else {
      const badgeWidth = Math.min(slot.width * .92, 280)
      badge = { x: slot.x + (slot.width - badgeWidth) / 2, y: slot.y, width: badgeWidth, height: 36, scale: 1 }
    }
    projects.push({ ...badge, id: group.id, count: family.filter(task => !task.contextOnly).length, stacked, mini })
    for (const task of family) {
      let pose: TaskPose = { id: task.id, projectId: group.id, x: 0, y: 0, width: cardWidth, height: memberHeight(task),
        scale: 1, depth: 0, hidden: false, mini, focused: task.id === taskId }
      if (task.id === taskId) {
        pose = { ...pose, x: 8, y: 66, width: readingWidth, height: focusHeight, mini: false }
      } else if (mini) {
        const node = miniature!.nodes.find(node => node.id === task.id)
        pose = { ...pose, x: badge.x + (node?.x || 0) * miniature!.scale, y: badge.y + 66 + (node?.y || 0) * miniature!.scale,
          width: (node?.width || 42) * miniature!.scale, height: (node?.height || 22) * miniature!.scale, scale: 1, hidden: !node }
      } else if (selectedProject) {
        const node = spreadNodes.get(task.id)!
        pose = { ...pose, ...node, scale: spread!.scale, projectOverview: true }
      } else {
        const node = slot.nodes?.find(node => node.id === task.id)
        pose = { ...pose, width: slot.cardWidth,
          x: slot.x + (node?.x || 0), y: slot.y + 62 + (node?.y || 0),
          height: node ? foldedHeight(task, node) : memberHeight(task), compactParent: node?.compactParent,
          depth: node?.depth || 0, hidden: node?.hidden || false, stackId: node?.stackId }
      }
      cards.push(pose)
    }
    // Child agents enter the graph only when selected and connect to their parent.
    const child = group.tasks.find(task => task.id === taskId && !family.some(root => root.id === task.id))
    if (child) cards.push({ id: child.id, projectId: group.id, x: 8, y: 66, width: readingWidth, height: focusHeight,
      scale: 1, depth: 0, hidden: false, mini: false, focused: true })
  })
  // Small primary fallback prioritises access. Five projects without scrolling is
  // guaranteed on the full portrait secondary display, not a 420px overlay.
  let compactY = 32
  if (compact && !projectId) projects.forEach(project => {
    project.x = 12; project.y = compactY; project.width = Math.min(width - 30, 280)
    const slot = slots.find(slot => slot.id === project.id)!
    for (const card of cards.filter(card => card.projectId === project.id)) card.y += compactY - slot.y
    compactY += slot.height + 30
  })
  const bottom = Math.max(height, ...cards.filter(card => !card.hidden).map(card => card.y + card.height * card.scale + 30))
  const readingTop = 66 + focusHeight + 16
  return { slots, projects, cards, readingWidth, readingTop, readingHeight: Math.max(200, (options.readingBottom ?? characterTop) - readingTop - 8),
    height: bottom, compact }
}
