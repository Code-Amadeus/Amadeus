import { useState, useEffect, useRef, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'

interface Props {
  role: 'user' | 'assistant' | 'system'
  text: string
  translation?: string
  streaming?: boolean
  onStreamIdle?: () => void
  userAvatar?: string
  assistantAvatar?: string
  hasPreviousMessage?: boolean
}

const DOT_PHASES = ['●', '● ●', '● ● ●', '● ●']

export default function ChatBubble({ role, text, translation = '', streaming, onStreamIdle, userAvatar = '', assistantAvatar = '', hasPreviousMessage }: Props) {
  const [display, setDisplay] = useState(text)
  const [dotPhase, setDotPhase] = useState(0)
  const [cursorVisible, setCursorVisible] = useState(true)
  const animRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined)
  const idleRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const rawRef = useRef(text)
  rawRef.current = text

  const tick = useCallback(() => {
    if (!rawRef.current) {
      setDotPhase(p => (p + 1) % 4)
    } else {
      setCursorVisible(v => !v)
    }
  }, [])

  // streaming: 420ms tick, 1500ms idle timeout (assistant only)
  useEffect(() => {
    if (streaming && role === 'assistant') {
      animRef.current = setInterval(tick, 420)
      if (text && idleRef.current) clearTimeout(idleRef.current)
      if (text) {
        idleRef.current = setTimeout(() => {
          if (animRef.current) clearInterval(animRef.current)
          setDisplay(text)
          onStreamIdle?.()
        }, 1500)
      }
    }
    return () => {
      if (animRef.current) clearInterval(animRef.current)
      if (idleRef.current) clearTimeout(idleRef.current)
    }
  }, [streaming, text, tick, onStreamIdle, role])

  // update display from state
  useEffect(() => {
    if (streaming && role === 'assistant') {
      setDisplay(!text ? DOT_PHASES[dotPhase % 4] : text + (cursorVisible ? '▋' : ' '))
    } else {
      setDisplay(text)
    }
  }, [text, streaming, role, dotPhase, cursorVisible])

  if (role === 'system') {
    return (
      <div
        className="flex justify-center px-[18px]"
        style={{ marginTop: hasPreviousMessage ? 10 : 0 }}
      >
        <span className="text-[12px] text-[#C42B1C] bg-[#FFF3CD]/60 rounded-lg px-4 py-1.5 max-w-[80%] text-center">
          {text}
        </span>
      </div>
    )
  }

  const isUser = role === 'user'
  const avatar = isUser ? userAvatar : assistantAvatar

  return (
    <div
      className="flex items-start gap-[10px]"
      style={{
        flexDirection: isUser ? 'row-reverse' : 'row',
        paddingInline: 'clamp(24px, 2.5vw, 34px)',
        marginTop: hasPreviousMessage ? 10 : 0,
      }}
    >
      {/* compact desktop avatar */}
      <div
        className="shrink-0 flex items-center justify-center text-[10px] select-none overflow-hidden"
        style={{
          width: 28, height: 28, borderRadius: 14,
          fontWeight: 600, fontFamily: 'var(--font)',
          ...(isUser
            ? { color: '#FFFFFF', backgroundColor: 'var(--accent)', border: '1px solid var(--accent)' }
            : { color: 'var(--accent)', backgroundColor: '#EEF2F6', border: '1px solid var(--border)' }
          ),
        }}
      >
        {avatar
          ? <img src={avatar} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          : isUser ? 'U' : 'K'}
      </div>

      {/* Keep short messages compact while long messages wrap within the chat lane. */}
      <div
        className="min-w-0"
        style={{
          flex: '0 1 auto',
          width: 'fit-content',
          maxWidth: '70%',
        }}
      >
        <div
          className="text-[12px] leading-[150%] whitespace-pre-wrap break-words select-text"
          style={{
            fontFamily: 'var(--font-cjk)',
            borderRadius: 8, padding: '8px 11px',
            ...(isUser
              ? {
                  color: '#FFFFFF', backgroundColor: 'var(--accent)',
                  boxShadow: '0 5px 16px rgba(17,24,39,0.09)',
                }
              : {
                  color: 'var(--text)', backgroundColor: 'var(--surface)',
                  border: '1px solid var(--border)',
                  boxShadow: '0 4px 14px rgba(17,24,39,0.07)',
                }
            ),
          }}
        >
          {isUser ? display : <ReactMarkdown className="markdown-body">{display}</ReactMarkdown>}
          {!isUser && translation ? (
            <div
              aria-label="Simplified Chinese translation"
              className="whitespace-pre-wrap break-words"
              style={{
                borderTop: '1px solid var(--border)',
                color: 'var(--muted)',
                fontSize: 11.5,
                lineHeight: '155%',
                marginTop: 7,
                paddingTop: 7,
              }}
            >
              {translation}
            </div>
          ) : null}
        </div>
      </div>

      {/* Fill the remaining lane so the bubble stays beside its own avatar. */}
      <div className="flex-1 min-w-0" />
    </div>
  )
}
