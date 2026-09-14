export interface Message {
  role: 'user' | 'assistant' | 'system'
  text: string
  turnId?: string
  streaming?: boolean
}

export const INTERRUPTED_MARKER = '[interrupted by user]'

export async function runSessionSelection(
  state: { pending: number },
  changed: (pending: boolean) => void,
  request: () => Promise<Record<string, unknown>>,
  apply: (payload: Record<string, unknown>) => void,
): Promise<Record<string, unknown>> {
  state.pending += 1
  changed(true)
  try {
    const payload = await request()
    if (payload.ok !== false) apply(payload)
    return payload
  } finally {
    state.pending -= 1
    changed(state.pending > 0)
  }
}

export function chatEventMatchesSession(
  payload: Record<string, unknown>, sessionId: string, interrupted: Set<string>,
): boolean {
  const turnId = typeof payload.turn_id === 'string' ? payload.turn_id : ''
  return Boolean(sessionId && payload.session_id === sessionId && turnId && !interrupted.has(turnId))
}

export function chatAsrDestination(source: unknown): 'composer' | 'direct' | 'ignore' {
  if (source === 'vn_player') return 'ignore'
  if (source === 'wake') return 'direct'
  return 'composer'
}

export function acceptedRoleMessage(
  payload: Record<string, unknown>, sessionId: string, interrupted: Set<string>,
): { messageId: string; text: string } | null {
  const messageId = typeof payload.message_id === 'string' ? payload.message_id : ''
  const text = typeof payload.text === 'string' ? payload.text : ''
  if (!sessionId || payload.session_id !== sessionId || !messageId || !text.trim()
    || interrupted.has(messageId)) return null
  return { messageId, text }
}

export function interruptedDisplayText(
  existingText: string,
  eventText: string,
  marker = INTERRUPTED_MARKER,
): string {
  const existing = String(existingText || '').trim()
  const incoming = String(eventText || '').trim()
  const tag = String(marker || INTERRUPTED_MARKER).trim()
  if (existing.includes(tag)) return existing
  if (!incoming || incoming === tag) return existing ? `${existing} ${tag}` : tag
  return incoming.includes(tag) ? incoming : `${incoming} ${tag}`
}

export function patchInterruptedMessage(
  prev: Message[],
  params: {
    turnId?: string
    activeTurnId?: string
    text?: string
    marker?: string
  },
): Message[] {
  const marker = params.marker || INTERRUPTED_MARKER
  const turnId = params.turnId || params.activeTurnId || ''
  const findByTurnId = (id: string) => (
    id ? prev.findIndex(m => m.role === 'assistant' && m.turnId === id) : -1
  )
  let index = findByTurnId(params.turnId || '')
  if (index < 0) index = findByTurnId(params.activeTurnId || '')
  if (index < 0) {
    for (let i = prev.length - 1; i >= 0; i -= 1) {
      if (prev[i]?.role === 'assistant') {
        index = i
        break
      }
    }
  }
  if (index >= 0) {
    const next = [...prev]
    next[index] = {
      ...next[index],
      turnId: next[index].turnId || turnId || undefined,
      text: interruptedDisplayText(next[index].text, params.text || '', marker),
      streaming: false,
    }
    return next
  }
  return [
    ...prev,
    {
      role: 'assistant',
      text: interruptedDisplayText('', params.text || '', marker),
      turnId: turnId || undefined,
      streaming: false,
    },
  ]
}
