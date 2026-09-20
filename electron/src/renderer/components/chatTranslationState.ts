import type { Message } from './chatMessageState'

export interface ChatTranslationCandidate {
  key: string
  text: string
  turnId: string
}

function textFingerprint(text: string): string {
  let hash = 0x811c9dc5
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(36)
}

export function chatTranslationKey(message: Message, index: number): string {
  if (message.role !== 'assistant' || message.streaming || !message.text.trim()) return ''
  const identity = message.turnId ? `turn:${message.turnId}` : `message:${index}`
  return `${identity}:${message.text.length}:${textFingerprint(message.text)}`
}

// Subtitle translation is a render-time extra for the live conversational
// flow.  A Session now reloads its complete transcript, so translating every
// message on load would turn one reload into unbounded derived model work.
// Only the most recent messages — one Main Chat prompt window — are
// candidates; older text stays plain rather than triggering new calls.
export const TRANSLATION_RECENT_MESSAGE_LIMIT = 20

export function chatTranslationCandidates(
  messages: Message[],
  recentLimit: number = TRANSLATION_RECENT_MESSAGE_LIMIT,
): ChatTranslationCandidate[] {
  const limit = Math.max(1, Math.trunc(recentLimit) || TRANSLATION_RECENT_MESSAGE_LIMIT)
  const start = Math.max(0, messages.length - limit)
  return messages.slice(start).flatMap((message, offset) => {
    const index = start + offset
    const key = chatTranslationKey(message, index)
    return key
      ? [{ key, text: message.text, turnId: message.turnId || '' }]
      : []
  })
}
