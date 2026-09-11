import type { FloatingCompanionTask } from './floatingCompanionState'

export type StackSelection = { taskId: string; attention: string; manual?: boolean }
export const STACK_LAYER_LIMIT = 7
export const STACK_STEP = 14
export const stackMetrics = (count: number) => ({ layers: Math.min(count, STACK_LAYER_LIMIT),
  tail: Math.max(0, Math.min(count, STACK_LAYER_LIMIT) - 1) * STACK_STEP, overflow: Math.max(0, count - STACK_LAYER_LIMIT) })

export function stackAttention(tasks: FloatingCompanionTask[], acknowledged: Set<string>) {
  return tasks.filter(task => !acknowledged.has(task.key) && ['attention', 'blocked'].includes(task.phase))
    .map(task => task.key).sort().join('|')
}

/** Manual choice survives ordinary updates. Only a new unresolved request can
 * displace it. Acknowledgement changes priority, never creates task activity. */
export function stackOrder(tasks: FloatingCompanionTask[], selection?: StackSelection, acknowledged = new Set<string>()) {
  const rank = (task: FloatingCompanionTask) => task.contextOnly ? 6 : acknowledged.has(task.key) ? 5
    : ({ attention: 0, blocked: 1, ready: 2, running: 3, idle: 4 }[task.phase])
  const attention = stackAttention(tasks, acknowledged)
  const selected = tasks.find(task => task.id === selection?.taskId)
  const knownAttention = new Set(selection?.attention.split('|') || [])
  const newAttention = attention.split('|').some(key => key && !knownAttention.has(key))
  const manual = selected && selection && !newAttention
    && (selection.manual !== false || rank(selected) <= Math.min(...tasks.map(rank)))
  return [...tasks].sort((a,b) => (manual ? Number(b.id === selection.taskId) - Number(a.id === selection.taskId) : 0)
    || rank(a) - rank(b) || (b.lastActivityAt || 0) - (a.lastActivityAt || 0) || a.id.localeCompare(b.id))
}
