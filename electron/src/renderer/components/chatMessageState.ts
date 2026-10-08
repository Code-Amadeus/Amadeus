export interface Message {
  role: 'user' | 'assistant' | 'system'
  text: string
  turnId?: string
  messageId?: string
  streaming?: boolean
}

export const INTERRUPTED_MARKER = '[interrupted by user]'

export function automaticSessionSelection<T extends {
  id: string; character_id?: string; timestamp?: number
}>(sessions: T[], currentCharacterId: unknown, currentSessionId = ''): T | undefined {
  if (typeof currentCharacterId !== 'string' || !currentCharacterId) {
    throw new Error('Chat character identity is unavailable. Reconnect to the backend.')
  }
  const owned = sessions.filter(session => session.character_id === currentCharacterId)
  return owned.find(session => session.id === currentSessionId)
    || [...owned].sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0))[0]
}

export async function runSessionSelection(
  state: { pending: number },
  changed: (pending: boolean) => void,
  request: () => Promise<Record<string, unknown>>,
  apply: (payload: Record<string, unknown>) => void,
  feedback?: (notice: string) => void,
  fallbackNotice = 'Could not switch chats.',
): Promise<Record<string, unknown>> {
  state.pending += 1
  changed(true)
  try {
    const payload = await request()
    if (payload.ok === false) {
      const reason = [payload.message, payload.detail, payload.error]
        .find(value => typeof value === 'string' && value.trim())
      feedback?.(typeof reason === 'string' ? reason : fallbackNotice)
    } else {
      apply(payload)
      feedback?.('')
    }
    return payload
  } catch (error) {
    feedback?.(error instanceof Error && error.message.trim()
      ? error.message : fallbackNotice)
    throw error
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
): { messageId: string; text: string; turnId?: string } | null {
  const messageId = typeof payload.message_id === 'string' ? payload.message_id : ''
  const text = typeof payload.text === 'string' ? payload.text : ''
  const turnId = typeof payload.turn_id === 'string' ? payload.turn_id : ''
  if (!sessionId || payload.session_id !== sessionId || !messageId || !text.trim()
    || interrupted.has(turnId || messageId)) return null
  return { messageId, text, ...(turnId ? { turnId } : {}) }
}

export function updateAssistantMessage(prev: Message[], turnId: string, text: string, streaming: boolean): Message[] {
  const index = prev.findIndex(m => m.role === 'assistant'
    && (m.messageId ? m.messageId === turnId : m.turnId === turnId))
  if (turnId && index >= 0) {
    const next = [...prev]
    next[index] = { ...next[index], text, streaming }
    return next
  }
  return [...prev, { role: 'assistant', text, turnId, streaming }]
}

export function applyRoleMessage(
  prev: Message[], line: { messageId: string; text: string; turnId?: string },
): Message[] {
  const turnId = line.turnId || line.messageId
  const index = prev.findIndex(m => m.role === 'assistant'
    && (m.messageId === line.messageId || (!m.messageId && m.turnId === turnId)))
  const message: Message = { role: 'assistant', text: line.text, turnId,
    messageId: line.messageId, streaming: false }
  if (index < 0) return [...prev, message]
  const next = [...prev]
  next[index] = message
  return next
}

export function finishAssistantTurn(prev: Message[], turnId: string, text: string): Message[] {
  // Role publications already own their final messages. A turn-completion
  // projection must not duplicate the head or overwrite its later Host reply.
  if (prev.some(m => m.role === 'assistant' && m.turnId === turnId && m.messageId)) {
    return prev.map(m => m.turnId === turnId && m.streaming ? { ...m, streaming: false } : m)
  }
  return updateAssistantMessage(prev, turnId, text, false)
}

export function assistantTurnAnchors(messages: Message[]): Map<string, number> {
  const anchors = new Map<string, number>()
  messages.forEach((message, index) => {
    if (message.role === 'assistant' && message.turnId) anchors.set(message.turnId, index)
  })
  return anchors
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
  const findByTurnId = (id: string) => id ? assistantTurnAnchors(prev).get(id) ?? -1 : -1
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
