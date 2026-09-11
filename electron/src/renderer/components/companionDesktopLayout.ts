import type { CompanionDesktop, LayoutPlacements, Point, SceneTransform } from './companionLayoutPreferences'
import type { NodePose } from './companionConstellationLayout'

export type DesktopNode = NodePose & { id: string; kind: 'projects' | 'tasks' }
type Box = Point & { width: number; height: number }
export const characterHeightFor = (home: Box) => Math.min(home.height * (home.width <= 700 || home.height <= 800 ? .52 : .58), 1180, home.width * 1.23)
export const nodeKey = (node: Pick<DesktopNode, 'kind' | 'id'>) => `${node.kind}:${node.id}`

/** All renderer positions are relative to the stable home frame, including
 * positions on other displays. Only the host owns native display geometry. */
export function displayForPoint(point: Point, desktop: CompanionDesktop) {
  const world = { x: desktop.home.x + point.x, y: desktop.home.y + point.y }
  const distance = (box: Box) => Math.hypot(Math.max(box.x - world.x, 0, world.x - box.x - box.width),
    Math.max(box.y - world.y, 0, world.y - box.y - box.height))
  return [...desktop.displays].sort((a,b) => distance(a.bounds)-distance(b.bounds))[0]
}
export function localWorkArea(display: CompanionDesktop['displays'][number], desktop: CompanionDesktop): Box {
  return { ...display.workArea, x: display.workArea.x - desktop.home.x, y: display.workArea.y - desktop.home.y }
}
export function nodesOnDisplay(nodes: DesktopNode[], displayId: number, desktop: CompanionDesktop) {
  return new Set(nodes.filter(node => displayForPoint({ x: node.x + node.width * node.scale / 2,
    y: node.y + node.height * node.scale / 2 }, desktop).id === displayId).map(nodeKey))
}
export function readDesktopNodes(root: HTMLElement, frame: HTMLElement): DesktopNode[] {
  const origin = frame.getBoundingClientRect()
  return [...root.querySelectorAll<HTMLElement>('[data-task-id], [data-project-id]')]
    .filter(element => getComputedStyle(element).visibility === 'visible')
    .map(element => {
      const box = element.getBoundingClientRect(), scale = box.width / element.offsetWidth
      return { id: element.dataset.taskId || element.dataset.projectId!, kind: element.dataset.taskId ? 'tasks' : 'projects',
        x: box.left - origin.left, y: box.top - origin.top, width: element.offsetWidth, height: element.offsetHeight, scale }
    })
}

/** Freeze untouched nodes in world space. Hierarchy still describes identity;
 * a node on another display must not follow a transform of its visible parent. */
export function placeSelection(before: LayoutPlacements | undefined, nodes: DesktopNode[], selected: Set<string>, transform: (node: DesktopNode) => Point & { scale: number }): LayoutPlacements {
  const result: LayoutPlacements = { projects: { ...before?.projects }, tasks: { ...before?.tasks } }
  for (const node of nodes) {
    const pose = selected.has(nodeKey(node)) ? transform(node) : node
    result[node.kind][node.id] = { x: pose.x, y: pose.y, scale: pose.scale }
  }
  return result
}
export function transformSceneNode(node: DesktopNode, before: SceneTransform, after: SceneTransform, width: number, height: number) {
  const ratio = after.scale / before.scale
  return { x: width / 2 + after.x + (node.x - width / 2 - before.x) * ratio,
    y: height + after.y + (node.y - height - before.y) * ratio, scale: node.scale * ratio }
}
export function inheritedPlacement<T extends NodePose & { id: string }>(pose: T, kind: DesktopNode['kind'], placements: LayoutPlacements | undefined,
  ancestors: { kind: DesktopNode['kind']; pose: NodePose & { id: string } }[]): T {
  const exact = placements?.[kind][pose.id]
  if (exact) return { ...pose, ...exact }
  for (const ancestor of ancestors) {
    const placed = placements?.[ancestor.kind][ancestor.pose.id]
    if (!placed) continue
    const ratio = placed.scale / ancestor.pose.scale
    return { ...pose, x: placed.x + (pose.x - ancestor.pose.x) * ratio,
      y: placed.y + (pose.y - ancestor.pose.y) * ratio, scale: pose.scale * ratio }
  }
  return pose
}
export function boundsOf(nodes: NodePose[]): Box {
  const x = Math.min(...nodes.map(node => node.x)), y = Math.min(...nodes.map(node => node.y))
  return { x, y, width: Math.max(...nodes.map(node => node.x + node.width * node.scale)) - x,
    height: Math.max(...nodes.map(node => node.y + node.height * node.scale)) - y }
}

/** Fit only the carried group on drop. Distant monitor nodes are not part of
 * these bounds and therefore neither shift nor shrink. */
export function fitCarriedScene(scene: SceneTransform, box: Box, target: Box, width: number, height: number): SceneTransform {
  const ratio = Math.min(1, (target.width - 24) / Math.max(1, box.width), (target.height - 24) / Math.max(1, box.height))
  const anchor = { x: width / 2 + scene.x, y: height + scene.y }
  const x = anchor.x + (box.x - anchor.x) * ratio, y = anchor.y + (box.y - anchor.y) * ratio
  const dx = Math.max(target.x + 12, Math.min(target.x + target.width - box.width * ratio - 12, x)) - x
  const dy = Math.max(target.y + 12, Math.min(target.y + target.height - box.height * ratio - 12, y)) - y
  return { x: scene.x + dx, y: scene.y + dy, scale: scene.scale * ratio }
}

export function recoverNodeDelta(nodes: DesktopNode[], delta: Point, target: Box): Point {
  // Retain the gesture's geometry, but do not drop the grab areas into desktop
  // gaps or outside the destination work area.
  const box = boundsOf(nodes)
  const fitX = box.width <= target.width - 24, fitY = box.height <= target.height - 24
  return { x: Math.max(target.x + (fitX ? 12 : 24 - box.width) - box.x,
      Math.min(target.x + target.width - (fitX ? box.width + 12 : 24) - box.x, delta.x)),
    y: Math.max(target.y + 12 - box.y,
      Math.min(target.y + target.height - (fitY ? box.height + 12 : 40) - box.y, delta.y)) }
}
