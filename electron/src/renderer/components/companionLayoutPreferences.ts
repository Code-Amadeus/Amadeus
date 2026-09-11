import type { OverviewSlot } from './companionConstellationLayout'

export type LayoutMode = 'natural' | 'ordered' | 'scattered'
export type EditMode = 'locked' | 'cards' | 'scene'
export type Point = { x: number; y: number }
export type SceneTransform = Point & { scale: number }
export type LayoutOffsets = { projects: Record<string, Point>; tasks: Record<string, Point> }
export type LayoutPlacements = { projects: Record<string, Point & { scale: number }>; tasks: Record<string, Point & { scale: number }> }
export type CompanionDesktop = ReturnType<typeof import('../../main/windowPlacement').resolveCompanionDesktop>
export type LayoutProfile = {
  seed: number; slots: OverviewSlot[]; offsets: LayoutOffsets; scene: SceneTransform
  placements?: LayoutPlacements
  stackFronts?: Record<string, import('./companionStack').StackSelection>
}
export type LayoutPreferences = { version: 1; mode: LayoutMode; displays: Record<string, Partial<Record<LayoutMode, LayoutProfile>>> }

export const LAYOUT_STORAGE_KEY = 'amadeus.companion.layout.v1'
export const freshLayoutProfile = (): LayoutProfile => ({ seed: 0, slots: [], offsets: { projects: {}, tasks: {} }, scene: { x: 0, y: 0, scale: 1 } })
export const freshLayoutPreferences = (): LayoutPreferences => ({ version: 1, mode: 'natural', displays: {} })

export function readLayoutPreferences(raw: string | null): LayoutPreferences {
  if (!raw) return freshLayoutPreferences()
  try {
    const value = JSON.parse(raw)
    if (value.version === 1 && ['natural', 'ordered', 'scattered'].includes(value.mode) && value.displays && typeof value.displays === 'object') return value
  } catch { /* A broken local preference must not prevent the desktop opening. */ }
  return freshLayoutPreferences()
}

export function saveLayoutProfile(preferences: LayoutPreferences, display: string, mode: LayoutMode, profile: LayoutProfile): LayoutPreferences {
  return { ...preferences, displays: { ...preferences.displays, [display]: { ...preferences.displays[display], [mode]: profile } } }
}

/** Offsets are relative to the owning node. Moving a parent automatically moves
 * existing AND newly arriving descendants, without copying task authority. */
export function taskLayoutOffset(task: { id: string; parentTaskId?: string }, tasks: { id: string; parentTaskId?: string }[], offsets: LayoutOffsets): Point {
  let x = 0, y = 0, current: typeof task | undefined = task
  const seen = new Set<string>()
  while (current && !seen.has(current.id)) {
    seen.add(current.id)
    x += offsets.tasks[current.id]?.x || 0
    y += offsets.tasks[current.id]?.y || 0
    current = tasks.find(item => item.id === current?.parentTaskId)
  }
  return { x, y }
}

/** Scene coordinates share one bottom-centre origin with the character. */
export function scenePoint(point: Point, scene: SceneTransform, width: number, height: number): Point {
  return { x: width / 2 + (point.x - width / 2) * scene.scale + scene.x, y: height + (point.y - height) * scene.scale + scene.y }
}
