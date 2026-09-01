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

export function chatTranslationCandidates(messages: Message[]): ChatTranslationCandidate[] {
  return messages.flatMap((message, index) => {
    const key = chatTranslationKey(message, index)
    return key
      ? [{ key, text: message.text, turnId: message.turnId || '' }]
      : []
  })
}
