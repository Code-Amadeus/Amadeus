import { useEffect, useRef, useState } from 'react'
import { advanceSpokenCaption, type SpokenCaption as Caption } from './spokenCaptionState'

type Subscribe = (method: string, listener: (params: Record<string, unknown>) => void) => () => void
export default function SpokenCaption({ subscribe }: { subscribe: Subscribe }) {
  const [caption, setCaption] = useState<Caption | null>(null)
  const [held, setHeld] = useState(false)
  const textRef = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    const unsub = ['tts.sentence_start', 'tts.turn_complete', 'tts.status'].map(method => subscribe(method, event => {
      setCaption(current => advanceSpokenCaption(current, method, event))
    }))
    return () => unsub.forEach(stop => stop())
  }, [subscribe])
  useEffect(() => {
    if (!held && textRef.current) textRef.current.scrollTop = textRef.current.scrollHeight
  }, [caption?.text, held])
  useEffect(() => {
    if (!caption || caption.status === 'speaking' || held) return
    const timer = window.setTimeout(() => setCaption(null), 60000)
    return () => window.clearTimeout(timer)
  }, [caption, held])
  if (!caption) return null
  return <aside className="companion-spoken-caption" data-companion-hit tabIndex={0}
    onMouseEnter={() => setHeld(true)} onMouseLeave={() => setHeld(false)}
    onFocus={() => setHeld(true)} onBlur={() => setHeld(false)} aria-label="Amadeus 实际播报日文">
    <header><span>AMADEUS <small>· 播报日文</small></span><span>{({ speaking: '正在说话', finished: '本次播报', interrupted: '本句已中断' })[caption.status]}</span></header>
    <p ref={textRef} lang="ja">{caption.text}</p>
  </aside>
}
