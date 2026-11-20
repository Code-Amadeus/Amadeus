import { useState, useEffect, useRef, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import { characterInitial, useActiveCharacter } from '../activeCharacter'
import { useI18n } from '../i18n'

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
  const { t } = useI18n()
  const activeCharacter = useActiveCharacter()
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
      <div className="chat-message-row" data-role="system" data-follows={hasPreviousMessage || undefined}>
        <span className="chat-system-message">
          {text}
        </span>
      </div>
    )
  }

  const isUser = role === 'user'
  const avatar = isUser ? userAvatar : assistantAvatar

  return (
    <div className="chat-message-row" data-role={role} data-follows={hasPreviousMessage || undefined}>
      <div className="chat-avatar">
        {avatar
          ? <img src={avatar} alt="" />
          : isUser ? 'U' : characterInitial(activeCharacter)}
      </div>

      {/* Keep short messages compact while long messages wrap within the chat lane. */}
      <div className="chat-bubble-lane">
        <div className="chat-bubble">
          {isUser ? display : <ReactMarkdown className="markdown-body">{display}</ReactMarkdown>}
          {!isUser && translation ? (
            <div aria-label={t('Translation')} className="chat-bubble-translation">
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
