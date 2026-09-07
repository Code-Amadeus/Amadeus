import type { FloatingCompanionTask } from './floatingCompanionState'

export const REMINDER_INTERVAL = 10 * 60 * 1000
export const TASK_INACTIVITY_MS = 8 * 60 * 60 * 1000

/** Presentation scheduling only: the source snapshot owns resolved/read facts. */
export class CompanionNotificationQueue {
  tasks = new Map<string, FloatingCompanionTask>()
  visible = new Map<string, FloatingCompanionTask>()
  queue: string[] = []
  due = new Map<string, number>()
  deliveries = new Map<string, number>()
  acknowledged = new Set<string>()
  faults = new Set<string>()
  activity = new Map<string, number>()
  dismissed: Map<string, number>
  fading = new Map<string, number>()
  held = new Set<string>()
  active: { task: FloatingCompanionTask; speaking: boolean } | null = null
  enabled: boolean
  constructor(enabled = true, dismissed: Record<string, number> = {}, history: Record<string, { count: number; acknowledged?: boolean; due?: number }> = {}) {
    this.enabled = enabled
    this.dismissed = new Map(Object.entries(dismissed))
    for (const [key, item] of Object.entries(history)) {
      if (!item || !Number.isInteger(item.count) || item.count < 0 || item.count > 2) continue
      this.deliveries.set(key, item.count)
      if (item.acknowledged) this.acknowledged.add(key)
      if (item.count === 1 && !item.acknowledged && Number.isFinite(item.due)) this.due.set(key, item.due!)
    }
  }

  history(): Record<string, { count: number; acknowledged: boolean; due?: number }> {
    return Object.fromEntries([...new Set([...this.deliveries.keys(), ...this.acknowledged])].map(key => [key, {
      count: this.deliveries.get(key) ?? 0, acknowledged: this.acknowledged.has(key), due: this.due.get(key),
    }]))
  }

  private available(task: FloatingCompanionTask, now: number): boolean {
    const at = this.activity.get(task.id)!
    return now < at + TASK_INACTIVITY_MS && at > (this.dismissed.get(task.id) ?? -Infinity)
  }

  update(tasks: FloatingCompanionTask[], now: number): void {
    const previous = this.tasks
    this.tasks = new Map(tasks.map(task => [task.key, task]))
    if (this.active) {
      this.active.task = this.tasks.get(this.active.task.key) ?? this.active.task
    }
    this.queue = this.queue.filter(key => this.tasks.has(key))
    if (previous.size) {
      for (const key of this.due.keys()) if (!this.tasks.has(key)) this.due.delete(key)
      for (const key of this.deliveries.keys()) if (!this.tasks.has(key)) this.deliveries.delete(key)
      for (const key of this.acknowledged) if (!this.tasks.has(key)) this.acknowledged.delete(key)
    }
    for (const key of this.faults) if (!this.tasks.has(key)) this.faults.delete(key)
    for (const key of this.fading.keys()) if (!this.tasks.has(key)) this.fading.delete(key)
    for (const id of this.held) if (!tasks.some(task => task.id === id)) this.held.delete(id)
    for (const [id, task] of this.visible) {
      if (!tasks.some(current => current.id === id)) this.visible.delete(id)
    }
    for (const task of tasks) {
      const prior = [...previous.values()].find(candidate => candidate.id === task.id)
      // Sources with timestamps own the clock. The existing ProviderRun list
      // can lack them; observe a changed task payload once, never every poll.
      const at = task.lastActivityAt ?? (
        !prior || prior.key !== task.key || prior.detail !== task.detail || prior.phase !== task.phase
          ? now : this.activity.get(task.id)!
      )
      this.activity.set(task.id, Math.max(this.activity.get(task.id) ?? -Infinity, at))
      if (this.activity.get(task.id)! > (this.dismissed.get(task.id) ?? Infinity)) this.dismissed.delete(task.id)
      // Visual identity is the task, speech identity is the notification key.
      // A phase change replaces the card in place, even while audio is queued.
      if (this.available(task, now) || this.visible.has(task.id)) this.visible.set(task.id, task)
      if (this.available(task, now) && this.enabled && task.phase !== 'running' && task.announce !== false && !previous.has(task.key) && !this.acknowledged.has(task.key) && !this.deliveries.has(task.key)) {
        this.queue.push(task.key)
      }
    }
    this.tick(now)
  }

  tick(now: number): boolean {
    let changed = false
    this.queue = this.queue.filter(key => {
      const task = this.tasks.get(key)
      return task && this.available(task, now)
    })
    for (const [id, task] of this.visible) {
      if (this.available(task, now) || this.held.has(id) || this.active?.task.id === id) {
        changed = this.fading.delete(task.key) || changed
        continue
      }
      if (!this.fading.has(task.key)) {
        this.fading.set(task.key, now + 400)
        changed = true
      } else if (now >= this.fading.get(task.key)!) {
        this.visible.delete(id)
        this.fading.delete(task.key)
        changed = true
      }
    }
    for (const [key, at] of this.enabled ? this.due : []) {
      const task = this.tasks.get(key)
      if (!task) continue
      if (task.announce === false || !this.available(task, now)) { this.due.delete(key); continue }
      if (at <= now && this.tasks.has(key) && !this.queue.includes(key) && this.active?.task.key !== key) {
        this.queue.push(key)
      }
    }
    return changed
  }

  hold(id: string, held: boolean): void {
    const task = this.visible.get(id)
    if (task) this.fading.delete(task.key)
    if (held) this.held.add(id)
    else this.held.delete(id)
  }

  dismiss(id: string): void {
    this.dismissed.set(id, this.activity.get(id)!)
    this.visible.delete(id)
    this.held.delete(id)
    this.queue = this.queue.filter(key => this.tasks.get(key)?.id !== id)
    for (const [key, task] of this.tasks) if (task.id === id) {
      this.due.delete(key)
      this.fading.delete(key)
    }
  }

  acknowledge(key: string): void {
    // Local notification acknowledgement only; never a Codex answer/read fact.
    this.acknowledged.add(key)
    this.queue = this.queue.filter(candidate => candidate !== key)
    this.due.delete(key)
  }

  take(): FloatingCompanionTask | null {
    if (!this.enabled || this.active) return null
    while (this.queue.length) {
      const task = this.tasks.get(this.queue.shift()!)
      if (task) { this.active = { task, speaking: false }; return task }
    }
    return null
  }

  started(key: string): void {
    if (this.active?.task.key !== key) return
    this.active.speaking = true
    this.faults.delete(key)
  }

  finished(key: string, now: number, fault = false): void {
    if (this.active?.task.key !== key) return
    const task = this.active.task
    if (fault) {
      this.faults.add(key)
    }
    this.active = null
    this.due.delete(key)
    const count = (this.deliveries.get(key) ?? 0) + 1
    if (this.tasks.has(key)) this.deliveries.set(key, count)
    if (count < 2 && this.tasks.has(key) && task.repeatable && task.announce !== false && !this.acknowledged.has(key) && this.enabled && this.available(task, now)) this.due.set(key, now + REMINDER_INTERVAL)
  }

  preempted(key: string): void {
    if (this.active?.task.key !== key) return
    this.active = null
    if (this.enabled && this.tasks.has(key) && !this.acknowledged.has(key) && !this.queue.includes(key)) this.queue.unshift(key)
  }

  setEnabled(enabled: boolean, now: number): void {
    this.enabled = enabled
    this.active = null
    this.queue = []
    this.due.clear()
    for (const task of this.tasks.values()) {
      if (enabled && this.deliveries.get(task.key) === 1 && task.repeatable && task.announce !== false && !this.acknowledged.has(task.key) && this.available(task, now)) this.due.set(task.key, now + REMINDER_INTERVAL)
    }
  }
}
