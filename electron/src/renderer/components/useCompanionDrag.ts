import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { Point } from './companionLayoutPreferences'

/** One pointer session, one coordinate space. Capture and a temporary hit region
 * prevent the native transparent window from passing through mid-drag. */
export function useCompanionDrag(enabled = true) {
  const [dragging, setDragging] = useState(false)
  const session = useRef<{ start: Point; delta: Point; move: (delta: Point) => void; cancel: () => void; complete?: (delta: Point, point: Point) => void; element: HTMLElement; pointer: number } | null>(null)
  const suppressClick = useRef(false)
  const clickReset = useRef(0)
  const frame = useRef(0)
  const begin = (event: ReactPointerEvent<HTMLElement>, move: (delta: Point) => void, cancel: () => void, complete?: (delta: Point, point: Point) => void) => {
    if (!enabled || event.button !== 0 || !event.isPrimary) return
    window.clearTimeout(clickReset.current)
    event.preventDefault(); event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    session.current = { start: { x: event.clientX, y: event.clientY }, delta: { x: 0, y: 0 }, move, cancel, complete, element: event.currentTarget, pointer: event.pointerId }
    suppressClick.current = false
    setDragging(true)
  }
  useEffect(() => {
    if (!enabled) return
    const move = (event: PointerEvent) => {
      const active = session.current
      if (!active || active.pointer !== event.pointerId) return
      active.delta.x = event.clientX - active.start.x; active.delta.y = event.clientY - active.start.y
      if (Math.hypot(active.delta.x, active.delta.y) > 4) suppressClick.current = true
      if (!frame.current) frame.current = requestAnimationFrame(() => { frame.current = 0; if (session.current && suppressClick.current) session.current.move(session.current.delta) })
    }
    const finish = (event: Event) => {
      const active = session.current
      if (!active || ('pointerId' in event && event.pointerId !== active.pointer) || ('key' in event && event.key !== 'Escape')) return
      cancelAnimationFrame(frame.current); frame.current = 0
      if (event.type === 'pointerup' && event instanceof PointerEvent) {
        active.delta = { x: event.clientX - active.start.x, y: event.clientY - active.start.y }
        if (suppressClick.current) {
          active.move(active.delta)
          active.complete?.(active.delta, { x: event.clientX, y: event.clientY })
        }
      }
      else active.cancel()
      session.current = null
      if (active.element.hasPointerCapture(active.pointer)) active.element.releasePointerCapture(active.pointer)
      // Suppression belongs to the drag's own click, not a later click on a
      // different node when the grab handle has already consumed that gesture.
      clickReset.current = window.setTimeout(() => { suppressClick.current = false }, 0)
      setDragging(false)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
    window.addEventListener('lostpointercapture', finish, true)
    window.addEventListener('blur', finish)
    window.addEventListener('keydown', finish)
    return () => {
      if (session.current) finish(new Event('pointercancel'))
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish); window.removeEventListener('keydown', finish)
      window.removeEventListener('lostpointercapture', finish, true); window.removeEventListener('blur', finish)
      cancelAnimationFrame(frame.current)
      window.clearTimeout(clickReset.current)
    }
  }, [enabled])
  return { begin, dragging, consumeClick: () => { const value = suppressClick.current; suppressClick.current = false; return value } }
}
