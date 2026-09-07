export type SpokenCaption = { turnId: string; sentenceId: string; text: string; status: 'speaking' | 'finished' | 'interrupted' }

/** Caption only sentences that have physically started, never model candidates. */
export function advanceSpokenCaption(current: SpokenCaption | null, method: string, event: Record<string, unknown>): SpokenCaption | null {
  if (method === 'tts.sentence_start') {
    const text = String(event.text || '').trim()
    const sentenceId = String(event.sentence_id || '')
    if (!text || current?.sentenceId === sentenceId) return current
    const turnId = String(event.turn_id || '')
    const append = current?.status === 'speaking' && current.turnId === turnId
    return { turnId, sentenceId, text: ((append ? current.text : '') + text).slice(-1800), status: 'speaking' }
  }
  if (!current) return null
  if (method === 'tts.turn_complete') return { ...current, status: 'finished' }
  if (method === 'tts.status' && event.status === 'interrupted') return { ...current, status: 'interrupted' }
  return current
}
