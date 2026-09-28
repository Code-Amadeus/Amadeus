export type VNActivity = {
  id: string; sessionId: string; seq: number; method: string; text: string; time: number
}

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? value as Record<string, unknown> : {}
const text = (value: unknown): string => typeof value === 'string' ? value.trim() : ''

export function activityFromEvent(method: string, payload: Record<string, unknown>): VNActivity | null {
  let content = ''
  if (method === 'vn.line') {
    const line = record(payload.line)
    content = [text(line.speaker), text(line.text)].filter(Boolean).join(': ')
  } else if (method === 'vn.reaction') {
    const reaction = record(payload.reaction)
    if (reaction.decision === 'speak') content = text(record(reaction.speak).text)
  } else if (method === 'vn.summary') {
    content = text(record(payload.scene_summary).summary)
  } else if (method === 'vn.player.event') {
    content = text(record(payload.event).text)
  } else if (method === 'vn.error') {
    content = text(payload.error)
  }
  if (!content || !payload.event_id) return null
  // Emotion instructions belong to presentation, not the readable transcript.
  content = content.replace(/\[EMO\b[^\]]*\]/gi, '').trim()
  return { id: String(payload.event_id), sessionId: String(payload.session_id || ''),
    seq: Number(payload.event_seq || 0), method, text: content, time: Number(payload.emitted_at_ms || 0) }
}

export function mergeActivity(current: VNActivity[], incoming: VNActivity[], sessionId: string): VNActivity[] {
  const items = new Map<string, VNActivity>()
  for (const item of [...current, ...incoming]) if (item.sessionId === sessionId) items.set(item.id, item)
  return [...items.values()].sort((a, b) => a.seq - b.seq).slice(-200)
}

export function sessionBanner(launch: Record<string, unknown>, runtime: Record<string, unknown> | null,
  connected: boolean, setupReady: boolean): { title: string; detail: string; tone: 'live' | 'waiting' | 'error' | 'idle' } {
  const state = String(launch.status || 'idle')
  const game = record(launch.game), hook = record(launch.hook), bridge = record(launch.bridge)
  if (!connected) return { title: 'Desktop connection unavailable', detail: 'Reconnect to the desktop service to manage VN Player.', tone: 'error' }
  if (state === 'error') return { title: 'Game connection failed', detail: 'Review the error below, then retry or edit this game.', tone: 'error' }
  if (state === 'starting') return { title: game.status === 'waiting_for_steam' ? 'Waiting for Steam to start the game…' : 'Connecting to game…', detail: 'You can cancel while the game connects.', tone: 'waiting' }
  if (state === 'stopping') return { title: 'Ending session…', detail: 'Releasing the connection and companion controls.', tone: 'waiting' }
  if (state !== 'active') return { title: setupReady ? 'Ready to connect' : 'Finish game setup', detail: setupReady ? 'Start the game connection, or test a few lines first.' : 'Add a game and connect its text source to begin.', tone: 'idle' }
  if (game.status === 'exited') return { title: 'Game has closed', detail: 'End this session, then start again when you are ready.', tone: 'error' }
  if (hook.status === 'exited') return { title: 'Text source has closed', detail: 'Reconnect to the game to resume text capture.', tone: 'error' }
  if (bridge.status !== 'running') return { title: 'Text connection interrupted', detail: 'Waiting for the text source. Check connection details or reconnect.', tone: 'waiting' }
  if (!Number(bridge.lineCount)) return { title: 'Connected · waiting for game text', detail: 'Advance a dialogue line. Menus and pauses may produce no text.', tone: 'waiting' }
  if (launch.captureOnly) return { title: 'Text received — compare it with the game', detail: 'Confirm dialogue, choices and repeated lines before starting your companion.', tone: 'live' }
  if (runtime?.status !== 'active') return { title: 'Companion is unavailable', detail: 'Text is connected. End the session and restart the companion.', tone: 'error' }
  if (record(runtime?.llm).configured === false || record(runtime?.llm).enabled === false || record(record(runtime?.capabilities).interaction).enabled === false) {
    return { title: 'Following text · model unavailable', detail: 'Text capture is working. Check the companion model before asking questions.', tone: 'waiting' }
  }
  if (record(runtime?.preferences).commentary_paused) return { title: 'Following quietly', detail: 'Story recording continues. You can still ask questions.', tone: 'live' }
  return { title: 'Following your game', detail: 'Your companion is receiving game text. Ask whenever you like.', tone: 'live' }
}
