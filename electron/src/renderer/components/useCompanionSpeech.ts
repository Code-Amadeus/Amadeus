import { useCallback, useEffect, useRef, useState } from 'react'
import type { FloatingCompanionTask } from './floatingCompanionState'
import { CompanionNotificationQueue } from './companionNotificationQueue'

type Send = (method: string, params: Record<string, unknown>) => Promise<Record<string, unknown>>
type Subscribe = (method: string, listener: (params: Record<string, unknown>) => void) => () => void
const VOICE_SETTING = 'amadeus.companion.reminderVoice'
const DISMISSED_SETTING = 'amadeus.companion.dismissedActivity'
const HISTORY_SETTING = 'amadeus.companion.notificationHistory'

function readHistory(): ReturnType<CompanionNotificationQueue['history']> {
  try { return JSON.parse(localStorage.getItem(HISTORY_SETTING) || '{}') || {} } catch { return {} }
}

function readDismissed(): Record<string, number> {
  try {
    const value = JSON.parse(localStorage.getItem(DISMISSED_SETTING) || '{}')
    return Object.fromEntries(Object.entries(value).filter(([, at]) => typeof at === 'number' && Number.isFinite(at))) as Record<string, number>
  } catch { return {} }
}

export function useCompanionSpeech(tasks: FloatingCompanionTask[], send: Send, subscribe: Subscribe, connected: boolean) {
  const [queue] = useState(() => new CompanionNotificationQueue(localStorage.getItem(VOICE_SETTING) === 'on', readDismissed(), readHistory()))
  const [audioNote, setAudioNote] = useState('')
  const [, redraw] = useState(0)
  const flight = useRef<{ id: string; key: string } | null>(null)
  const refresh = useCallback(() => {
    localStorage.setItem(HISTORY_SETTING, JSON.stringify(queue.history()))
    redraw(value => value + 1)
  }, [queue])

  const drive = useCallback(() => {
    if (queue.tick(Date.now())) refresh()
    if (!connected || flight.current) return
    const task = queue.take()
    if (!task) return
    const request = { id: crypto.randomUUID(), key: task.key }
    flight.current = request
    void send('companion.speak', {
      request_id: request.id, text: task.detail || task.title, ended: task.phase === 'ready',
      project_name: task.projectName || '', provider: task.provider,
    })
      .then(result => {
        if (flight.current !== request) return
        if (result.status === 'accepted') return
        flight.current = null
        if (result.status === 'busy') queue.preempted(task.key)
        else queue.finished(task.key, Date.now(), true)
        refresh()
      }).catch(() => {
        if (flight.current !== request) return
        flight.current = null
        queue.finished(task.key, Date.now(), true)
        refresh()
      })
  }, [connected, queue, refresh, send])

  useEffect(() => {
    queue.update(tasks, Date.now())
    const current = flight.current
    if (current && !queue.tasks.has(current.key) && !queue.active?.speaking) {
      flight.current = null
      queue.preempted(current.key)
      void send('companion.cancel', { request_id: current.id }).catch(() => {})
    }
    refresh()
    drive()
  }, [tasks, queue, send, refresh, drive])

  useEffect(() => subscribe('companion.voice', event => {
    const current = flight.current
    if (!current || event.request_id !== current.id) return
    if (event.status === 'started') queue.started(current.key)
    else {
      flight.current = null
      if (event.status === 'preempted' || event.status === 'deferred') queue.preempted(current.key)
      else queue.finished(current.key, Date.now(), event.status === 'unavailable')
    }
    refresh()
    // Drain the next ready item as soon as playback ends; the timer is only
    // needed for foreground-busy retries and scheduled repeat reminders.
    if (event.status !== 'started') drive()
  }), [subscribe, queue, refresh, drive])

  useEffect(() => {
    const update = (event: Record<string, unknown>) => setAudioNote(String(event.note || ''))
    const unsubscribe = subscribe('companion.audio-activity', update)
    if (connected) void send('companion.audio-activity', {}).then(update).catch(() => {})
    return unsubscribe
  }, [connected, send, subscribe])

  useEffect(() => {
    if (!connected && flight.current) {
      queue.finished(flight.current.key, Date.now(), true)
      flight.current = null
      refresh()
    }
    const timer = window.setInterval(drive, 1000)
    return () => window.clearInterval(timer)
  }, [connected, drive, queue, refresh])

  useEffect(() => () => {
    if (flight.current) void send('companion.cancel', { request_id: flight.current.id }).catch(() => {})
  }, [send])

  const toggleVoice = useCallback(() => {
    const current = flight.current
    flight.current = null
    queue.setEnabled(!queue.enabled, Date.now())
    localStorage.setItem(VOICE_SETTING, queue.enabled ? 'on' : 'off')
    if (current) void send('companion.cancel', { request_id: current.id }).catch(() => {})
    refresh()
  }, [queue, send, refresh])

  const holdCard = useCallback((id: string, held: boolean) => { queue.hold(id, held); refresh() }, [queue, refresh])
  const dismissCard = useCallback((id: string) => {
    queue.dismiss(id)
    localStorage.setItem(DISMISSED_SETTING, JSON.stringify(Object.fromEntries(queue.dismissed)))
    const current = flight.current
    if (current && queue.active?.task.id === id && !queue.active.speaking) {
      flight.current = null
      queue.active = null
      void send('companion.cancel', { request_id: current.id }).catch(() => {})
    }
    refresh()
    drive()
  }, [queue, send, refresh, drive])
  const acknowledge = useCallback((key: string) => {
    queue.acknowledge(key)
    const current = flight.current
    if (current?.key === key && !queue.active?.speaking) {
      flight.current = null
      queue.active = null
      void send('companion.cancel', { request_id: current.id }).catch(() => {})
    }
    refresh()
  }, [queue, send, refresh])
  const visible = [...queue.visible.values()]
  return { visible, holdCard, dismissCard, acknowledge, acknowledged: queue.acknowledged, audioNote, fading: queue.fading, voiceEnabled: queue.enabled, toggleVoice, faults: queue.faults, speakingKey: queue.active?.speaking ? queue.active.task.key : '' }
}
