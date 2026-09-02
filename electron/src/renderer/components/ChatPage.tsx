import { useState, useCallback, useEffect, useRef } from 'react'
import { Fragment, useMemo } from 'react'
import type {
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
} from 'react'
import ChatBubble from './ChatBubble'
import ChatSessionRail, {
  type ChatProjectSummary,
  type ChatSessionSummary,
} from './ChatSessionRail'
import ChatWorkActivityCard from './ChatWorkActivityCard'
import FluentIcon from './FluentIcon'
import ProjectAppsPanel, { type ProjectAppSummary } from './ProjectAppsPanel'
import CrtWorkWidget from './work/CrtWorkWidget'
import {
  activitiesFromProviderRuns,
  applyProviderEvent,
  applyProviderResult,
  type ChatWorkActivityRun,
} from './chatWorkActivity'
import {
  INTERRUPTED_MARKER,
  patchInterruptedMessage,
  type Message,
} from './chatMessageState'
import {
  chatTranslationCandidates,
  chatTranslationKey,
} from './chatTranslationState'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
  renderActive: boolean       // false=VTS, true=PixiJS
  renderAssetUrl: string      // URL served by backend AssetServer
}

interface VisualAttachment {
  name: string
  mime: string
  dataUrl: string
  width: number
  height: number
  byteLength: number
}

interface VisionWindowItem {
  hwnd: string
  title: string
  processName?: string
  pid?: number
  selected?: boolean
  rect?: {
    left: number
    top: number
    width: number
    height: number
  }
}

function toVisionWindowItems(items: unknown): VisionWindowItem[] {
  if (!Array.isArray(items)) return []
  return items
    .map((item: any) => ({
      hwnd: String(item?.hwnd ?? ''),
      title: String(item?.title ?? '').trim(),
      processName: String(item?.processName ?? '').trim(),
      pid: Number(item?.pid ?? 0) || undefined,
      selected: Boolean(item?.selected),
      rect: item?.rect && typeof item.rect === 'object'
        ? {
            left: Number(item.rect.left ?? 0),
            top: Number(item.rect.top ?? 0),
            width: Number(item.rect.width ?? 0),
            height: Number(item.rect.height ?? 0),
          }
        : undefined,
    }))
    .filter(item => item.hwnd && item.title)
}

const CHAT_PANEL_DEFAULT_W = 560
const CHAT_PANEL_MIN_W = 420
const CHARACTER_MIN_W = 360
const SPLIT_WIDTH_STORAGE_KEY = 'amadeus.render.chatWidth'
const MULTIMODAL_CHAT_PROVIDERS = new Set(['openai', 'gemini', 'hybrid3'])
const VISION_LONG_PRESS_MS = 560
const DRAFT_APPS_VIEW_ID = '__draft_apps__'
const VISUAL_ATTACHMENT_MAX_LONG_SIDE = 1280
const VISUAL_ATTACHMENT_JPEG_QUALITY = 0.82
const RENDER_BRIDGE_MESSAGE = 'amadeus.render.event'
const RENDER_EVENT_METHODS = [
  'render.emotion',
  'render.speaking',
  'render.mouth',
  'render.subtitle',
  'render.sprite_frames',
  'render.mode',
  'render.idle_animation',
  'render.idle_frame_interval',
  'render.sprite_clip_config',
  'render.mouth_config',
  'render.spriteforge_graph',
  'render.spriteforge_intent',
  'render.spriteforge_release',
  'render.hold_frame',
  'render.clear_hold',
] as const
const CRT_WORK_WIDGET_DEMO_ENABLED = (
  import.meta.env.DEV
  && new URLSearchParams(window.location.search).get('workDemo') === '1'
)

function clampChatWidth(width: number, totalWidth = Number.POSITIVE_INFINITY): number {
  const max = Number.isFinite(totalWidth)
    ? Math.max(CHAT_PANEL_MIN_W, totalWidth - CHARACTER_MIN_W)
    : 760
  return Math.min(Math.max(width, CHAT_PANEL_MIN_W), max)
}

function loadImageElement(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('Could not decode image'))
    image.src = src
  })
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('Could not read image'))
    reader.readAsDataURL(file)
  })
}

async function prepareImageAttachment(file: File): Promise<VisualAttachment> {
  if (!file.type.startsWith('image/')) {
    throw new Error('Please choose an image file.')
  }
  const originalDataUrl = await readFileAsDataUrl(file)
  const image = await loadImageElement(originalDataUrl)
  const scale = Math.min(1, VISUAL_ATTACHMENT_MAX_LONG_SIDE / Math.max(image.naturalWidth, image.naturalHeight, 1))
  const width = Math.max(1, Math.round(image.naturalWidth * scale))
  const height = Math.max(1, Math.round(image.naturalHeight * scale))
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Canvas is unavailable.')
  ctx.drawImage(image, 0, 0, width, height)
  const dataUrl = canvas.toDataURL('image/jpeg', VISUAL_ATTACHMENT_JPEG_QUALITY)
  const base64 = dataUrl.split(',', 2)[1] || ''
  const byteLength = Math.floor((base64.length * 3) / 4)
  return {
    name: file.name || 'image.jpg',
    mime: 'image/jpeg',
    dataUrl,
    width,
    height,
    byteLength,
  }
}

export default function ChatPage({ send, subscribe, connected, renderActive, renderAssetUrl }: Props) {
  const [messages, setMessages] = useState<Message[]>([])
  const [chatTranslationEnabled, setChatTranslationEnabled] = useState(false)
  const [chatTranslations, setChatTranslations] = useState<Record<string, string>>({})
  const [workActivities, setWorkActivities] = useState<ChatWorkActivityRun[]>([])
  const [streamingText, setStreamingText] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [input, setInput] = useState('')
  const [provider, setProvider] = useState('deepseek')
  const [chatAvatars, setChatAvatars] = useState({ user: '', assistant: '' })
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([])
  const [projects, setProjects] = useState<ChatProjectSummary[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [projectCorrectionOpen, setProjectCorrectionOpen] = useState(false)
  const [projectViewId, setProjectViewId] = useState('')
  const [projectApps, setProjectApps] = useState<ProjectAppSummary[]>([])
  const [projectAppsComplete, setProjectAppsComplete] = useState(true)
  const [projectAppsLoading, setProjectAppsLoading] = useState(false)
  const [projectAppsFeedback, setProjectAppsFeedback] = useState('')
  const [projectAppAction, setProjectAppAction] = useState('')
  const [artifactContext, setArtifactContext] = useState<{
    projectId: string
    workItemId: string
    artifactId: string
    title: string
  } | null>(null)
  const [asrListening, setAsrListening] = useState(false)
  const [pixiSubmode, setPixiSubmode] = useState('graph')
  const [pendingVisualAttachment, setPendingVisualAttachment] = useState<VisualAttachment | null>(null)
  const [visionVideoMode, setVisionVideoMode] = useState(false)
  const [visionWindowPickerOpen, setVisionWindowPickerOpen] = useState(false)
  const [visionWindowLoading, setVisionWindowLoading] = useState(false)
  const [visionWindowError, setVisionWindowError] = useState('')
  const [visionWindows, setVisionWindows] = useState<VisionWindowItem[]>([])
  const [chatWidth, setChatWidth] = useState(() => {
    const saved = Number(localStorage.getItem(SPLIT_WIDTH_STORAGE_KEY))
    return Number.isFinite(saved) && saved > 0 ? clampChatWidth(saved) : CHAT_PANEL_DEFAULT_W
  })
  const [isSplitResizing, setIsSplitResizing] = useState(false)
  const splitRootRef = useRef<HTMLDivElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const imageInputRef = useRef<HTMLInputElement>(null)
  const renderFrameRef = useRef<HTMLIFrameElement>(null)
  const interruptedTurnIdsRef = useRef<Set<string>>(new Set())
  const activeStreamTurnIdRef = useRef('')
  const streamingTextRef = useRef('')
  const lastAssistantTurnIdRef = useRef('')
  const chatTranslationRequestedRef = useRef<Set<string>>(new Set())
  const chatTranslationGenerationRef = useRef(0)
  const activeSessionRef = useRef('')
  const visionPressTimerRef = useRef<number | null>(null)
  const visionLongPressRef = useRef(false)
  const canUseMultimodal = MULTIMODAL_CHAT_PROVIDERS.has(provider)

  const toMessages = useCallback((items: unknown): Message[] => {
    if (!Array.isArray(items)) return []
    return items
      .map((m: any) => ({
        role: (m.role === 'assistant' || m.role === 'system') ? m.role : 'user',
        text: String(m.content ?? m.text ?? ''),
        turnId: String(m.turn_id ?? m.turnId ?? '') || undefined,
        streaming: false,
      }))
      .filter(m => m.text)
  }, [])

  const upsertAssistantMessage = useCallback((turnId: string, text: string, streamingValue: boolean) => {
    setMessages(prev => {
      if (turnId) {
        const index = prev.findIndex(m => m.role === 'assistant' && m.turnId === turnId)
        if (index >= 0) {
          const next = [...prev]
          next[index] = { ...next[index], text, streaming: streamingValue }
          return next
        }
      }
      return [...prev, { role: 'assistant', text, turnId, streaming: streamingValue }]
    })
  }, [])

  const refreshSessions = useCallback(async () => {
    const res = await send('session.list', {})
    const list = Array.isArray(res.sessions) ? res.sessions as unknown as ChatSessionSummary[] : []
    setSessions(list)
    if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
    return {
      list,
      currentSessionId: String(res.current_session_id || ''),
    }
  }, [send])

  const applySessionPayload = useCallback((res: Record<string, unknown>) => {
    if (Array.isArray(res.sessions)) setSessions(res.sessions as unknown as ChatSessionSummary[])
    if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
    const session = res.session as ChatSessionSummary | undefined
    const sessionId = String(res.current_session_id || session?.id || '')
    if (sessionId) {
      if (activeSessionRef.current && activeSessionRef.current !== sessionId) {
        setWorkActivities([])
      }
      activeSessionRef.current = sessionId
      setActiveSession(sessionId)
    }
    if (Array.isArray(res.messages)) {
      chatTranslationGenerationRef.current += 1
      chatTranslationRequestedRef.current.clear()
      setChatTranslations({})
      setMessages(toMessages(res.messages))
    }
    setStreaming(false)
    setStreamingText('')
    streamingTextRef.current = ''
  }, [toMessages])

  const hydrateWorkActivities = useCallback(async (sessionId: string) => {
    if (!sessionId) {
      setWorkActivities([])
      return
    }
    try {
      const res = await send('provider.activity.list', { session_id: sessionId })
      if (activeSessionRef.current !== sessionId) return
      setWorkActivities(activitiesFromProviderRuns(res.runs, sessionId))
    } catch {
      if (activeSessionRef.current === sessionId) setWorkActivities([])
    }
  }, [send])

  const loadSession = useCallback(async (id: string) => {
    const res = await send('session.load', { session_id: id })
    if (res.ok === false) return
    applySessionPayload(res)
    const session = res.session as ChatSessionSummary | undefined
    const sessionId = String(res.current_session_id || session?.id || id)
    await hydrateWorkActivities(sessionId)
  }, [send, applySessionPayload, hydrateWorkActivities])

  useEffect(() => {
    void window.amadeus?.getChatAvatars().then(value => {
      if (value) setChatAvatars(value)
    }).catch(error => console.error('[chat-avatar] load failed', error))
  }, [])

  useEffect(() => {
    if (!connected) return
    let cancelled = false
    const applyConfig = (payload: Record<string, unknown>) => {
      const values = payload.values && typeof payload.values === 'object'
        ? payload.values as Record<string, unknown>
        : payload
      const enabled = values.chat_translation_subtitles_enabled === true
      if (!cancelled) setChatTranslationEnabled(enabled)
    }
    const unsubscribe = subscribe('system.config', applyConfig)
    send('system.get_config', {}).then(applyConfig).catch(() => {
      if (!cancelled) setChatTranslationEnabled(false)
    })
    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [connected, send, subscribe])

  useEffect(() => {
    if (!chatTranslationEnabled) {
      chatTranslationGenerationRef.current += 1
      chatTranslationRequestedRef.current.clear()
      setChatTranslations({})
      return
    }

    const generation = chatTranslationGenerationRef.current
    for (const candidate of chatTranslationCandidates(messages)) {
      if (chatTranslationRequestedRef.current.has(candidate.key)) continue
      chatTranslationRequestedRef.current.add(candidate.key)
      void send('chat.translate', {
        text: candidate.text,
        turn_id: candidate.turnId,
      }).then(response => {
        if (generation !== chatTranslationGenerationRef.current) return
        const translation = String(response.translation ?? '').trim()
        if (!translation) return
        setChatTranslations(previous => ({
          ...previous,
          [candidate.key]: translation,
        }))
      }).catch(() => {
        if (generation === chatTranslationGenerationRef.current) {
          chatTranslationRequestedRef.current.delete(candidate.key)
        }
      })
    }
  }, [chatTranslationEnabled, messages, send])

  // auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingText])

  useEffect(() => {
    activeSessionRef.current = activeSession || ''
  }, [activeSession])

  useEffect(() => {
    localStorage.setItem(SPLIT_WIDTH_STORAGE_KEY, String(Math.round(chatWidth)))
  }, [chatWidth])

  const postRenderEvent = useCallback((method: string, params: Record<string, unknown>) => {
    renderFrameRef.current?.contentWindow?.postMessage({
      type: RENDER_BRIDGE_MESSAGE,
      method,
      params,
    }, '*')
  }, [])

  const handleRenderFrameLoad = useCallback(() => {
    if (!renderActive || !renderAssetUrl) return
    send('render.ready', {}).catch(error => {
      console.error('[render-bridge] state replay failed', error)
    })
  }, [renderActive, renderAssetUrl, send])

  useEffect(() => {
    if (!renderActive) return
    const unsubs = RENDER_EVENT_METHODS.map(method => (
      subscribe(method, params => postRenderEvent(method, params))
    ))
    return () => unsubs.forEach(unsubscribe => unsubscribe())
  }, [postRenderEvent, renderActive, subscribe])

  useEffect(() => {
    if (!renderActive) return

    const clampToContainer = () => {
      const total = splitRootRef.current?.getBoundingClientRect().width
      if (!total) return
      setChatWidth(width => clampChatWidth(width, total))
    }

    clampToContainer()
    window.addEventListener('resize', clampToContainer)
    return () => window.removeEventListener('resize', clampToContainer)
  }, [renderActive])

  useEffect(() => {
    if (!isSplitResizing) return

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const handlePointerMove = (event: PointerEvent) => {
      const root = splitRootRef.current
      if (!root) return
      const rect = root.getBoundingClientRect()
      const nextWidth = rect.right - event.clientX
      setChatWidth(clampChatWidth(nextWidth, rect.width))
    }

    const handlePointerUp = () => setIsSplitResizing(false)

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', handlePointerUp)
    window.addEventListener('pointercancel', handlePointerUp)

    return () => {
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', handlePointerUp)
      window.removeEventListener('pointercancel', handlePointerUp)
    }
  }, [isSplitResizing])

  // restore legacy persisted sessions
  useEffect(() => {
    if (!connected) return
    let cancelled = false
    ;(async () => {
      try {
        const { list, currentSessionId } = await refreshSessions()
        if (cancelled) return
        if (list.length > 0) {
          const current = list.find(session => session.id === currentSessionId)
          const latest = current || [...list].sort((a, b) => (Number(b.timestamp || 0) - Number(a.timestamp || 0)))[0]
          await loadSession(latest.id)
          return
        }
        const res = await send('session.create', {})
        if (cancelled) return
        const session = res.session as ChatSessionSummary | undefined
        const list2 = Array.isArray(res.sessions) ? res.sessions as unknown as ChatSessionSummary[] : (session ? [session] : [])
        setSessions(list2)
        if (session) {
          activeSessionRef.current = session.id
          setActiveSession(session.id)
        }
        setMessages([])
        setWorkActivities([])
      } catch {
        // Keep the chat usable even if the session API is temporarily unavailable.
      }
    })()
    return () => { cancelled = true }
  }, [connected, refreshSessions, loadSession, send])

  // load initial config + subscribe to config changes (cross-page sync)
  useEffect(() => {
    if (!connected) return
    send('system.get_config', {}).then(res => {
      if (res?.llm_provider) setProvider(String(res.llm_provider))
      if (res?.vision_mode) setVisionVideoMode(String(res.vision_mode) === 'watching')
    }).catch(() => {})

    const unsub = subscribe('system.config', (p) => {
      const values = (p.values ?? p) as Record<string, unknown>
      if (values.llm_provider !== undefined) setProvider(String(values.llm_provider))
      if (values.vision_mode !== undefined) setVisionVideoMode(String(values.vision_mode) === 'watching')
      if (values.vision_enabled !== undefined && !values.vision_enabled) setVisionVideoMode(false)
    })
    return unsub
  }, [connected, subscribe, send])

  useEffect(() => {
    if (!canUseMultimodal) {
      setPendingVisualAttachment(null)
      setVisionWindowPickerOpen(false)
      setVisionVideoMode(prev => {
        if (prev) {
          send('system.set_config', { values: { vision_enabled: false, vision_mode: 'off' } }).catch(() => {})
        }
        return false
      })
    }
  }, [canUseMultimodal, send])

  // subscribe to backend events
  useEffect(() => {
    const unsubs: Array<() => void> = []
    unsubs.push(subscribe('chat.token', (p) => {
      const turnId = String(p.turn_id ?? '')
      if (turnId && interruptedTurnIdsRef.current.has(turnId)) return
      if (turnId) activeStreamTurnIdRef.current = turnId
      streamingTextRef.current = String(p.token ?? '')
      setStreaming(true)
      setStreamingText(streamingTextRef.current)
      upsertAssistantMessage(turnId, streamingTextRef.current, true)
    }))
    unsubs.push(subscribe('chat.complete', (p) => {
      const text = String(p.full_text ?? p.token ?? '')
      const turnId = String(p.turn_id ?? '')
      if (turnId && interruptedTurnIdsRef.current.has(turnId)) {
        setStreaming(false)
        setStreamingText('')
        streamingTextRef.current = ''
        if (activeStreamTurnIdRef.current === turnId) activeStreamTurnIdRef.current = ''
        return
      }
      upsertAssistantMessage(turnId, text, false)
      setStreaming(false)
      setStreamingText('')
      streamingTextRef.current = ''
      if (turnId) lastAssistantTurnIdRef.current = turnId
      if (activeStreamTurnIdRef.current === turnId) activeStreamTurnIdRef.current = ''
      refreshSessions().catch(() => {})
    }))
    unsubs.push(subscribe('chat.observer_decision', (p) => {
      if (p.append_to_main_chat !== true) return
      const sessionId = String(p.session_id ?? '')
      if (!sessionId || sessionId !== activeSessionRef.current) return
      const text = String(p.main_chat_entry ?? '').trim()
      if (!text) return
      const attemptId = String(p.attempt_id ?? '')
      const runId = String(p.run_id ?? '')
      const workItemId = String(p.work_item_id ?? '')
      const messageId = String(p.message_id ?? '')
      const identity = messageId || attemptId || runId || workItemId
      if (!identity) return
      const action = String(p.action ?? 'update')
      const noteCount = String(p.note_count ?? '')
      upsertAssistantMessage(`work-observer:${identity}:${action}:${noteCount}`, text, false)
    }))
    unsubs.push(subscribe('chat.interrupted', (p) => {
      console.info('[ChatPage] chat.interrupted', p)
      const text = String(p.text ?? p.completed_text ?? '').trim()
      const marker = String(p.marker ?? INTERRUPTED_MARKER)
      const eventTurnId = String(p.turn_id ?? '')
      const activeStreamTurnId = activeStreamTurnIdRef.current
      const streamedText = streamingTextRef.current.trim()
      const turnId = eventTurnId || activeStreamTurnId || lastAssistantTurnIdRef.current
      if (turnId) interruptedTurnIdsRef.current.add(turnId)
      setMessages(prev => patchInterruptedMessage(prev, {
        turnId: eventTurnId,
        activeTurnId: activeStreamTurnId,
        text: text || streamedText || marker,
        marker,
      }))
      setStreaming(false)
      setStreamingText('')
      streamingTextRef.current = ''
      if (activeStreamTurnIdRef.current === turnId) activeStreamTurnIdRef.current = ''
      if (turnId) lastAssistantTurnIdRef.current = turnId
    }))
    unsubs.push(subscribe('chat.error', (p) => {
      setMessages(prev => [...prev, { role: 'system', text: `Error: ${p.error}` }])
      setStreaming(false)
      setStreamingText('')
      streamingTextRef.current = ''
    }))
    unsubs.push(subscribe('provider.event', (p) => {
      const metadata = p.metadata && typeof p.metadata === 'object'
        ? p.metadata as Record<string, unknown>
        : {}
      const sessionId = String(metadata.session_id || metadata.sessionId || '')
      if (!sessionId || sessionId !== activeSessionRef.current) return
      setWorkActivities(prev => applyProviderEvent(prev, p))
    }))
    unsubs.push(subscribe('provider.result', (p) => {
      const metadata = p.metadata && typeof p.metadata === 'object'
        ? p.metadata as Record<string, unknown>
        : {}
      const sessionId = String(metadata.session_id || metadata.sessionId || '')
      if (!sessionId || sessionId !== activeSessionRef.current) return
      setWorkActivities(prev => applyProviderResult(prev, p))
    }))
    unsubs.push(subscribe('session.changed', (p) => {
      applySessionPayload(p)
      const session = p.session as ChatSessionSummary | undefined
      const sessionId = String(p.current_session_id || session?.id || '')
      if (sessionId) void hydrateWorkActivities(sessionId)
    }))
    unsubs.push(subscribe('asr.recognized', (p) => {
      const text = String(p.text ?? '')
      if (text && p.is_final) {
        if (p.source === 'vn_player') {
          return
        }
        if (p.source === 'wake') {
          setMessages(prev => [...prev, { role: 'user', text }])
          setAsrListening(false)
          return
        }
        setInput(prev => prev ? `${prev} ${text}` : text)
        setAsrListening(false)
      }
    }))
    unsubs.push(subscribe('asr.status', (p) => {
      if (p.status === 'idle') setAsrListening(false)
    }))
    return () => unsubs.forEach(fn => fn())
  }, [subscribe, refreshSessions, upsertAssistantMessage, applySessionPayload, hydrateWorkActivities])

  const handleSend = useCallback(async () => {
    const typedText = input.trim()
    const text = typedText || (pendingVisualAttachment ? '请看这张图片。' : '')
    if (!text || !connected) return
    const visual =
      canUseMultimodal && pendingVisualAttachment
        ? {
            request: true,
            mode: 'attachment',
            scope: 'user_image',
            provider,
            source: 'user_image',
            attachment: {
              name: pendingVisualAttachment.name,
              byteLength: pendingVisualAttachment.byteLength,
            },
            frame: {
              mime: pendingVisualAttachment.mime,
              dataUrl: pendingVisualAttachment.dataUrl,
              width: pendingVisualAttachment.width,
              height: pendingVisualAttachment.height,
              byteLength: pendingVisualAttachment.byteLength,
            },
          }
        : undefined
    let sessionId = activeSession
    if (!sessionId) {
      try {
        const res = await send('session.create', {})
        const session = res.session as ChatSessionSummary | undefined
        if (session) {
          sessionId = session.id
          applySessionPayload(res)
        }
      } catch {
        // Send can still proceed without persistence if the backend is mid-reconnect.
      }
    }
    setInput('')
    setMessages(prev => [...prev, { role: 'user', text }])
    setStreamingText('')
    streamingTextRef.current = ''
    setStreaming(false)
    try {
      const turnId = crypto.randomUUID()
      interruptedTurnIdsRef.current.delete(turnId)
      activeStreamTurnIdRef.current = turnId
      const response = await send('chat.send', {
        text,
        provider,
        turn_id: turnId,
        session_id: sessionId || '',
        ...(visual ? { visual } : {}),
      })
      if (response.control === 'wake') {
        activeStreamTurnIdRef.current = ''
        setMessages(prev => {
          const last = prev[prev.length - 1]
          return last?.role === 'user' && last.text === text ? prev.slice(0, -1) : prev
        })
      }
      if (pendingVisualAttachment) setPendingVisualAttachment(null)
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Send failed: backend unreachable' }])
    }
  }, [input, connected, canUseMultimodal, pendingVisualAttachment, activeSession, send, provider, applySessionPayload])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }, [handleSend])

  const handleMicToggle = useCallback(async () => {
    if (!connected) return
    try {
      if (asrListening) {
        await send('asr.stop', {})
        setAsrListening(false)
      } else {
        await send('asr.start', {})
        setAsrListening(true)
      }
    } catch { /* ignore */ }
  }, [connected, send, asrListening])

  const clearVisionPressTimer = useCallback(() => {
    if (visionPressTimerRef.current !== null) {
      window.clearTimeout(visionPressTimerRef.current)
      visionPressTimerRef.current = null
    }
  }, [])

  useEffect(() => () => clearVisionPressTimer(), [clearVisionPressTimer])

  const handleVisionVideoToggle = useCallback(async () => {
    if (!connected || !canUseMultimodal) return
    const next = !visionVideoMode
    setVisionVideoMode(next)
    setPendingVisualAttachment(null)
    try {
      await send('system.set_config', {
        values: next
          ? {
              vision_enabled: true,
              vision_mode: 'watching',
              vision_provider: provider,
              vision_scope: 'full_screen',
            }
          : {
              vision_enabled: false,
              vision_mode: 'off',
            },
      })
    } catch {
      setVisionVideoMode(!next)
    }
  }, [connected, canUseMultimodal, visionVideoMode, send, provider])

  const loadVisionWindows = useCallback(async () => {
    setVisionWindowLoading(true)
    setVisionWindowError('')
    try {
      const res = await send('system.list_windows', { limit: 48 })
      setVisionWindows(toVisionWindowItems(res.windows))
    } catch (error) {
      setVisionWindows([])
      setVisionWindowError(error instanceof Error ? error.message : 'Could not list windows')
    } finally {
      setVisionWindowLoading(false)
    }
  }, [send])

  const handleVisionContextMenu = useCallback((event: ReactMouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    if (!connected || !canUseMultimodal) return
    clearVisionPressTimer()
    visionLongPressRef.current = false
    setVisionWindowPickerOpen(true)
    loadVisionWindows().catch(() => {})
  }, [connected, canUseMultimodal, clearVisionPressTimer, loadVisionWindows])

  const handleVisionWindowSelect = useCallback(async (windowItem: VisionWindowItem) => {
    if (!connected || !canUseMultimodal) return
    setVisionWindowError('')
    try {
      await send('system.set_config', {
        values: {
          vision_enabled: true,
          vision_mode: visionVideoMode ? 'watching' : 'on_demand',
          vision_provider: provider,
          vision_scope: 'selected_window',
          vision_window_handle: windowItem.hwnd,
        },
      })
      setVisionWindows(prev => prev.map(item => ({ ...item, selected: item.hwnd === windowItem.hwnd })))
      setVisionWindowPickerOpen(false)
      setPendingVisualAttachment(null)
    } catch (error) {
      setVisionWindowError(error instanceof Error ? error.message : 'Could not switch window')
    }
  }, [connected, canUseMultimodal, send, visionVideoMode, provider])

  const handleVisionFullScreenSelect = useCallback(async () => {
    if (!connected || !canUseMultimodal) return
    setVisionWindowError('')
    try {
      await send('system.set_config', {
        values: {
          vision_enabled: true,
          vision_mode: visionVideoMode ? 'watching' : 'on_demand',
          vision_provider: provider,
          vision_scope: 'full_screen',
          vision_window_handle: '',
        },
      })
      setVisionWindows(prev => prev.map(item => ({ ...item, selected: false })))
      setVisionWindowPickerOpen(false)
    } catch (error) {
      setVisionWindowError(error instanceof Error ? error.message : 'Could not switch to full screen')
    }
  }, [connected, canUseMultimodal, send, visionVideoMode, provider])

  const handleVisionPointerDown = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!connected || !canUseMultimodal) return
    if (event.button === 2) return
    event.preventDefault()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    visionLongPressRef.current = false
    clearVisionPressTimer()
    visionPressTimerRef.current = window.setTimeout(() => {
      visionLongPressRef.current = true
      handleVisionVideoToggle().catch(() => {})
    }, VISION_LONG_PRESS_MS)
  }, [connected, canUseMultimodal, clearVisionPressTimer, handleVisionVideoToggle])

  const handleVisionPointerUp = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!connected || !canUseMultimodal) return
    if (event.button === 2) return
    event.preventDefault()
    const wasLongPress = visionLongPressRef.current
    clearVisionPressTimer()
    visionLongPressRef.current = false
    if (!wasLongPress) {
      imageInputRef.current?.click()
    }
  }, [connected, canUseMultimodal, clearVisionPressTimer])

  const handleVisionPointerCancel = useCallback(() => {
    clearVisionPressTimer()
    visionLongPressRef.current = false
  }, [clearVisionPressTimer])

  const handleVisionFileChange = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !canUseMultimodal) return
    try {
      const attachment = await prepareImageAttachment(file)
      setPendingVisualAttachment(attachment)
    } catch (error) {
      setMessages(prev => [
        ...prev,
        { role: 'system', text: error instanceof Error ? error.message : 'Could not attach image' },
      ])
    }
  }, [canUseMultimodal])

  const handleProviderChange = useCallback(async (v: string) => {
    try {
      const res = await send('system.set_config', { values: { llm_provider: v } })
      const values = res.values as Record<string, unknown> | undefined
      if (values?.llm_provider) setProvider(String(values.llm_provider))
    } catch {
      setMessages(prev => [...prev, {
        role: 'system',
        text: 'Model switch failed; wait for the current reply to finish and try again.',
      }])
    }
  }, [send])

  const handleNewSession = useCallback(async () => {
    try {
      const res = await send('session.create', {})
      applySessionPayload(res)
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Could not create session' }])
    }
  }, [send, applySessionPayload])

  const handleNewProjectSession = useCallback(async (projectId: string) => {
    const project = projects.find(candidate => candidate.projectId === projectId)
    try {
      const res = await send('session.create', {
        project_id: projectId,
        title: project?.name || 'Project',
      })
      if (res.ok === false) {
        setMessages(prev => [...prev, {
          role: 'system',
          text: String(res.message || res.error || 'Could not create the Project chat'),
        }])
        return
      }
      applySessionPayload(res)
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Could not create the Project chat' }])
    }
  }, [applySessionPayload, projects, send])

  const handleNewProject = useCallback(async () => {
    try {
      const selection = await window.amadeus?.selectProjectDirectory()
      if (!selection || selection.cancelled) return
      if (!selection.ok || !selection.path) {
        throw new Error(selection.detail || 'Could not select a Project directory')
      }
      const res = await send('project.create', { workspace_path: selection.path })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not create the Project'))
      }
      applySessionPayload(res)
    } catch (error) {
      setMessages(prev => [...prev, {
        role: 'system',
        text: error instanceof Error ? error.message : String(error),
      }])
    }
  }, [applySessionPayload, send])

  const loadProjectApps = useCallback(async (projectId: string) => {
    if (!projectId) return
    setProjectAppsLoading(true)
    setProjectAppsFeedback('')
    try {
      const res = await send('project.apps.list', { project_id: projectId, limit: 100 })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not load Project apps'))
      }
      setProjectApps(Array.isArray(res.apps) ? res.apps as unknown as ProjectAppSummary[] : [])
      setProjectAppsComplete(res.complete !== false)
    } catch (error) {
      setProjectApps([])
      setProjectAppsComplete(true)
      setProjectAppsFeedback(error instanceof Error ? error.message : String(error))
    } finally {
      setProjectAppsLoading(false)
    }
  }, [send])

  const loadDraftApps = useCallback(async () => {
    setProjectAppsLoading(true)
    setProjectAppsFeedback('')
    try {
      const res = await send('draft.apps.list', { limit: 5 })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not load recent Draft apps'))
      }
      setProjectApps(Array.isArray(res.apps) ? res.apps as unknown as ProjectAppSummary[] : [])
      setProjectAppsComplete(res.complete !== false)
    } catch (error) {
      setProjectApps([])
      setProjectAppsComplete(true)
      setProjectAppsFeedback(error instanceof Error ? error.message : String(error))
    } finally {
      setProjectAppsLoading(false)
    }
  }, [send])

  const handleOpenProject = useCallback((projectId: string) => {
    setProjectViewId(projectId)
    setProjectApps([])
    setProjectAppsFeedback('')
    void loadProjectApps(projectId)
  }, [loadProjectApps])

  const handleOpenDraftApps = useCallback(() => {
    setProjectViewId(DRAFT_APPS_VIEW_ID)
    setProjectApps([])
    setProjectAppsFeedback('')
    void loadDraftApps()
  }, [loadDraftApps])

  const openArtifactConversation = useCallback(async (
    projectId: string,
    app: ProjectAppSummary,
  ) => {
    const res = await send('session.open_context', {
      project_id: app.projectId || projectId,
      work_item_id: app.workItemId,
    })
    if (res.ok === false) {
      throw new Error(String(res.message || res.error || 'Could not open the Artifact conversation'))
    }
    applySessionPayload(res)
  }, [applySessionPayload, send])

  const interactWithProjectApp = useCallback(async (app: ProjectAppSummary) => {
    if (!projectViewId || projectAppAction || streaming) return
    setProjectAppAction(`interact:${app.workItemId}`)
    setProjectAppsFeedback('')
    try {
      await openArtifactConversation(projectViewId, app)
      setArtifactContext({
        projectId: app.projectId || projectViewId,
        workItemId: app.workItemId,
        artifactId: app.artifactId,
        title: app.title,
      })
      setProjectViewId('')
      window.setTimeout(() => inputRef.current?.focus(), 0)
    } catch (error) {
      setProjectAppsFeedback(error instanceof Error ? error.message : String(error))
    } finally {
      setProjectAppAction('')
    }
  }, [openArtifactConversation, projectAppAction, projectViewId, streaming])

  const openProjectApp = useCallback(async (app: ProjectAppSummary) => {
    if (!projectViewId || projectAppAction || streaming) return
    setProjectAppAction(`open:${app.workItemId}`)
    setProjectAppsFeedback('')
    try {
      await openArtifactConversation(projectViewId, app)
      const prepared = await send('auip.attach.prepare', {
        artifact_id: app.artifactId,
        mode: 'observe',
      })
      if (prepared.ok === false) {
        throw new Error(String(prepared.detail || prepared.error || 'Could not prepare the AUIP application'))
      }
      const launchUrl = String(prepared.launch_url || '')
      const hostSurfaceId = String(prepared.host_surface_id || '')
      const workItemId = String(prepared.work_item_id || app.workItemId)
      if (!launchUrl || !hostSurfaceId) {
        throw new Error('The Host did not return a complete AUIP launch descriptor.')
      }
      const opened = await window.amadeus?.openAuipApp(launchUrl, hostSurfaceId, workItemId)
      if (!opened?.ok) {
        throw new Error(opened?.detail || 'The desktop Host refused the AUIP application.')
      }
      setProjectViewId('')
    } catch (error) {
      setProjectAppsFeedback(error instanceof Error ? error.message : String(error))
    } finally {
      setProjectAppAction('')
    }
  }, [openArtifactConversation, projectAppAction, projectViewId, send, streaming])

  const promoteDraftApp = useCallback(async (app: ProjectAppSummary) => {
    if (projectViewId !== DRAFT_APPS_VIEW_ID || projectAppAction || streaming) return
    setProjectAppAction(`promote:${app.workItemId}`)
    setProjectAppsFeedback('')
    try {
      let sourceLoaded = false
      if (app.sourceSessionId) {
        const source = await send('session.load', { session_id: app.sourceSessionId })
        if (source.ok !== false) {
          applySessionPayload(source)
          sourceLoaded = true
        }
      }
      if (!sourceLoaded) {
        await openArtifactConversation(app.projectId, app)
      }
      const promotedResponse = await send('work.promote', { work_item_id: app.workItemId })
      if (promotedResponse.ok === false) {
        throw new Error(String(promotedResponse.message || promotedResponse.error || 'Could not promote the Draft app'))
      }
      await refreshSessions()
      const promoted = promotedResponse.promoted && typeof promotedResponse.promoted === 'object'
        ? promotedResponse.promoted as Record<string, unknown>
        : {}
      const promotedProjectId = String(promoted.projectId || promoted.project_id || '')
      setArtifactContext(null)
      if (promotedProjectId) {
        setProjectViewId(promotedProjectId)
        await loadProjectApps(promotedProjectId)
      } else {
        setProjectViewId('')
      }
    } catch (error) {
      setProjectAppsFeedback(error instanceof Error ? error.message : String(error))
    } finally {
      setProjectAppAction('')
    }
  }, [applySessionPayload, loadProjectApps, openArtifactConversation, projectAppAction, projectViewId, refreshSessions, send, streaming])

  const correctProjectBinding = useCallback(async (projectId: string) => {
    setProjectCorrectionOpen(false)
    if (!activeSession) return
    const currentProjectId = String(
      sessions.find(session => session.id === activeSession)?.context?.projectId || '',
    )
    if (projectId === currentProjectId) return
    try {
      const res = await send('session.correct_project', {
        session_id: activeSession,
        project_id: projectId,
      })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not move the chat'))
      }
      applySessionPayload(res)
    } catch (error) {
      setMessages(prev => [...prev, {
        role: 'system',
        text: error instanceof Error ? error.message : String(error),
      }])
    }
  }, [activeSession, applySessionPayload, send, sessions])

  const promoteActiveDraft = useCallback(async () => {
    const context = sessions.find(session => session.id === activeSession)?.context
    if (!context?.workItemId || !context.canPromoteToProject) return
    try {
      const res = await send('work.promote', { work_item_id: context.workItemId })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not promote the Draft'))
      }
      await refreshSessions()
      const promoted = res.promoted && typeof res.promoted === 'object'
        ? res.promoted as Record<string, unknown>
        : {}
      const projectId = String(promoted.projectId || promoted.project_id || '')
      if (projectId) handleOpenProject(projectId)
    } catch (error) {
      setMessages(prev => [...prev, {
        role: 'system',
        text: error instanceof Error ? error.message : String(error),
      }])
    }
  }, [activeSession, handleOpenProject, refreshSessions, send, sessions])

  useEffect(() => {
    setProjectCorrectionOpen(false)
  }, [activeSession])

  const handleDeleteSession = useCallback(async (id: string) => {
    try {
      const res = await send('session.delete', { session_id: id })
    if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
      const next = Array.isArray(res.sessions) ? res.sessions as unknown as ChatSessionSummary[] : sessions.filter(s => s.id !== id)
      setSessions(next)
      if (activeSession === id) {
        const latest = [...next].sort((a, b) => Number(b.timestamp || 0) - Number(a.timestamp || 0))[0]
        if (latest) {
          await loadSession(latest.id)
        } else {
          setActiveSession(null)
          setMessages([])
          setStreamingText('')
          streamingTextRef.current = ''
          setStreaming(false)
        }
      }
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Could not delete session' }])
    }
  }, [activeSession, loadSession, send, sessions])

  const handleRenameSession = useCallback(async (id: string, currentTitle: string) => {
    const title = window.prompt('Rename session', currentTitle)?.trim()
    if (!title) return
    try {
      const res = await send('session.rename', { session_id: id, title })
      if (Array.isArray(res.sessions)) setSessions(res.sessions as unknown as ChatSessionSummary[])
      if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Could not rename session' }])
    }
  }, [send])

  const handlePixiSubmodeCycle = useCallback(() => {
    setPixiSubmode(prev => {
      const next = prev === 'graph' ? 'sprite' : prev === 'sprite' ? 'hybrid' : 'graph'
      send('expression.set_backend', { backend: 'graph', submode: next }).catch(() => {})
      return next
    })
  }, [send])

  const handleSplitPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!renderActive) return
    event.preventDefault()
    setIsSplitResizing(true)
  }, [renderActive])

  const pixiSubmodeLabel = pixiSubmode === 'graph' ? 'SpriteForge graph' : pixiSubmode === 'sprite' ? 'Sprite frames' : 'Live2D idle + frames'
  const activeContext = sessions.find(session => session.id === activeSession)?.context || null
  const projectView = projectViewId === DRAFT_APPS_VIEW_ID
    ? { projectId: '', name: 'Drafts' }
    : projects.find(project => project.projectId === projectViewId) || null

  useEffect(() => {
    if (artifactContext && activeContext?.workItemId !== artifactContext.workItemId) {
      setArtifactContext(null)
    }
  }, [activeContext?.workItemId, artifactContext])

  const visualButtonDisabled = !connected || !canUseMultimodal
  const visualButtonTitle = !canUseMultimodal
    ? 'Select OpenAI, Gemini, or hybrid3 to use visual input'
    : visionVideoMode
      ? 'Global vision watching is on. Hold to turn it off. Right-click to choose a window.'
      : pendingVisualAttachment
        ? `${pendingVisualAttachment.name} will be sent with the next message. Click to choose another image; hold for watching; right-click for window.`
        : 'Choose an image for the next message. Hold for watching; right-click for window.'

  const activitiesByTurn = useMemo(() => {
    const grouped = new Map<string, ChatWorkActivityRun[]>()
    for (const activity of workActivities) {
      const current = grouped.get(activity.turnId) || []
      current.push(activity)
      grouped.set(activity.turnId, current)
    }
    return grouped
  }, [workActivities])

  const comboCls = `text-[10px] border border-[var(--border)] rounded-md px-2
    bg-[var(--surface)] text-[var(--text)] outline-none
    hover:border-[var(--border-strong)]`

  const toolBtnStyle = (size: number): React.CSSProperties => ({
    width: size, height: size, display: 'flex', alignItems: 'center',
    justifyContent: 'center', border: 'none', background: 'transparent',
    color: 'var(--muted)', cursor: 'pointer', borderRadius: 8,
  })

  /* Chat panel (right side) */
  const chatPanel = (
    <div
      className="flex min-w-0"
      style={{
        minWidth: renderActive ? CHAT_PANEL_MIN_W : 0,
        width: renderActive ? chatWidth : '100%',
        flex: renderActive ? undefined : 1,
        padding: renderActive ? '12px 12px 12px 6px' : 14,
      }}
      >
      <div
        className="flex min-w-0 w-full h-full"
        style={{
          backgroundColor: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 14,
          overflow: 'hidden',
          boxShadow: '0 10px 30px rgba(17,24,39,0.06)',
        }}
      >
      <ChatSessionRail
        sessions={sessions}
        projects={projects}
        activeId={activeSession}
        artifactViewId={projectViewId === DRAFT_APPS_VIEW_ID ? 'drafts' : projectViewId}
        onSelect={id => { setProjectViewId(''); void loadSession(id) }}
        onNew={() => { setProjectViewId(''); void handleNewSession() }}
        onNewProject={() => { void handleNewProject() }}
        onNewProjectSession={id => { void handleNewProjectSession(id) }}
        onOpenProject={handleOpenProject}
        onOpenDraftApps={handleOpenDraftApps}
        onRename={handleRenameSession}
        onDelete={id => { void handleDeleteSession(id) }}
      />

      <div className="flex flex-col min-w-0 flex-1 h-full relative">
      {projectView && (
        <ProjectAppsPanel
          project={projectView}
          scope={projectViewId === DRAFT_APPS_VIEW_ID ? 'drafts' : 'project'}
          apps={projectApps}
          complete={projectAppsComplete}
          loading={projectAppsLoading}
          actionKey={streaming ? 'chat-busy' : projectAppAction}
          feedback={projectAppsFeedback}
          onBack={() => setProjectViewId('')}
          onRefresh={() => {
            if (projectViewId === DRAFT_APPS_VIEW_ID) void loadDraftApps()
            else void loadProjectApps(projectView.projectId)
          }}
          onNewChat={() => {
            setProjectViewId('')
            if (projectViewId === DRAFT_APPS_VIEW_ID) void handleNewSession()
            else void handleNewProjectSession(projectView.projectId)
          }}
          onOpen={app => { void openProjectApp(app) }}
          onInteract={app => { void interactWithProjectApp(app) }}
          onPromote={app => { void promoteDraftApp(app) }}
        />
      )}
      {/* Current conversation bar */}
      <div
        className="flex items-center gap-2 shrink-0"
        style={{ backgroundColor: 'var(--surface)', borderBottom: '1px solid var(--border)', padding: '5px 14px' }}
      >
        <span className="flex-1 text-[11px] font-[500] truncate" style={{ color: 'var(--text)' }}>
          {activeSession ? sessions.find(s => s.id === activeSession)?.title ?? 'Chat' : 'Chat'}
        </span>
        {projectCorrectionOpen ? (
          <select
            autoFocus
            aria-label="Move chat to Project"
            defaultValue=""
            onBlur={() => setProjectCorrectionOpen(false)}
            onChange={event => {
              const value = event.target.value
              if (value) void correctProjectBinding(value === '__draft__' ? '' : value)
            }}
            disabled={!connected || streaming}
            className={comboCls}
            style={{ width: 148, height: 28 }}
          >
            <option value="" disabled>Move chat…</option>
            <option value="__draft__">Draft</option>
            {projects.map(project => (
              <option key={project.projectId} value={project.projectId}>{project.name}</option>
            ))}
          </select>
        ) : (
          <div
            className="group flex items-center shrink-0"
            aria-label="Current chat project"
            title={activeContext?.projectId
              ? `This chat is bound to ${activeContext.projectName}`
              : 'This is a default Draft chat'}
            style={{
              height: 28,
              padding: '0 8px',
              borderRadius: 7,
              border: '1px solid var(--border)',
              backgroundColor: 'var(--surface-alt)',
              color: 'var(--muted)',
              fontSize: 10,
              gap: 6,
            }}
          >
            <span className="truncate" style={{ maxWidth: 120 }}>
              {activeContext?.projectId ? activeContext.projectName : 'Draft'}
            </span>
            {!activeContext?.projectId && activeContext?.canPromoteToProject && (
              <button
                type="button"
                onClick={() => { void promoteActiveDraft() }}
                title="Promote this Draft to a Project"
                className="opacity-0 group-hover:opacity-100 border-none bg-transparent cursor-pointer"
                style={{ color: 'var(--accent)', fontSize: 10, fontWeight: 600 }}
              >
                Promote
              </button>
            )}
            {activeSession && (
              <button
                type="button"
                onClick={() => setProjectCorrectionOpen(true)}
                title="Move this chat and preserve its history. Existing Work keeps its original Project; moving waits for active Work to finish."
                className="opacity-0 group-hover:opacity-100 border-none bg-transparent cursor-pointer"
                style={{ color: 'var(--faint)', fontSize: 10 }}
              >
                Move
              </button>
            )}
          </div>
        )}
        <button
          type="button"
          onClick={() => { void handleNewSession() }}
          title="New chat"
          aria-label="New chat"
          className="flex items-center justify-center rounded-md border-none bg-transparent text-[var(--muted)] hover:bg-[var(--hover)] hover:text-[var(--text)] cursor-pointer shrink-0"
          style={{ width: 30, height: 30, borderRadius: 6 }}
        >
          <FluentIcon name="Edit" size={16} />
        </button>
      </div>

      {/* Chat messages */}
      <div
        className="chat-scroll-area flex-1 overflow-y-auto"
        style={{
          backgroundColor: 'var(--bg)',
        }}
      >
        <div className="flex flex-col" style={{ padding: '14px 0 12px 0' }}>
          {messages.length === 0 && !streaming && (
            <p className="text-center mt-20" style={{ color: 'var(--faint)', fontSize: 13 }}>Type a message to start.</p>
          )}
          {messages.map((msg, i) => {
            const key = `${msg.role}-${msg.turnId || 'local'}-${i}`
            const attached = msg.role === 'assistant' && msg.turnId
              ? activitiesByTurn.get(msg.turnId) || []
              : []
            return (
              <Fragment key={key}>
                <ChatBubble
                  role={msg.role}
                  text={msg.text}
                  translation={chatTranslations[chatTranslationKey(msg, i)] || ''}
                  streaming={msg.streaming}
                  userAvatar={chatAvatars.user}
                  assistantAvatar={chatAvatars.assistant}
                />
                {attached.map(activity => (
                  <ChatWorkActivityCard key={activity.runId} activity={activity} />
                ))}
              </Fragment>
            )
          })}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input bar */}
      <div className="shrink-0" style={{ backgroundColor: 'var(--bg)', padding: '10px 14px 14px 14px' }}>
        <div
          className="transition-colors"
          style={{ backgroundColor: 'var(--surface-alt)', border: '1px solid var(--border)', borderRadius: 18, boxShadow: '0 5px 18px rgba(17,24,39,0.06)' }}
          onMouseEnter={e => {
            (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--surface)'
            ;(e.currentTarget as HTMLElement).style.borderColor = 'var(--border-strong)'
          }}
          onMouseLeave={e => {
            (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--surface-alt)'
            ;(e.currentTarget as HTMLElement).style.borderColor = 'var(--border)'
          }}
        >
          <div style={{ padding: '10px 11px 9px 14px' }}>
            {artifactContext && (
              <div
                className="flex items-center gap-2"
                style={{
                  marginBottom: 8,
                  padding: '6px 8px',
                  borderRadius: 9,
                  border: '1px solid color-mix(in srgb, var(--accent) 30%, var(--border))',
                  backgroundColor: 'var(--surface)',
                  color: 'var(--muted)',
                  fontSize: 10,
                }}
              >
                <FluentIcon name="Chat" size={13} style={{ color: 'var(--accent)' }} />
                <span className="truncate">
                  Interacting with Amadeus about <strong style={{ color: 'var(--text)' }}>{artifactContext.title}</strong>
                </span>
                <button
                  type="button"
                  onClick={() => setArtifactContext(null)}
                  title="Dismiss Artifact context label"
                  aria-label="Dismiss Artifact context label"
                  style={{ marginLeft: 'auto', border: 'none', background: 'transparent', color: 'var(--faint)', cursor: 'pointer' }}
                >
                  x
                </button>
              </div>
            )}
            {pendingVisualAttachment && (
              <div
                className="flex items-center gap-2"
                style={{
                  marginBottom: 8,
                  padding: '6px 8px',
                  borderRadius: 9,
                  border: '1px solid var(--border)',
                  backgroundColor: 'var(--surface)',
                  color: 'var(--muted)',
                  fontSize: 11,
                }}
              >
                <FluentIcon name="Photo" size={14} />
                <span className="truncate" style={{ color: 'var(--text)', maxWidth: 190 }}>
                  {pendingVisualAttachment.name}
                </span>
                <span className="shrink-0" style={{ color: 'var(--faint)' }}>
                  {pendingVisualAttachment.width}x{pendingVisualAttachment.height}
                </span>
                <button
                  type="button"
                  title="Remove image"
                  onClick={() => setPendingVisualAttachment(null)}
                  style={{
                    marginLeft: 'auto',
                    border: 'none',
                    background: 'transparent',
                    color: 'var(--faint)',
                    cursor: 'pointer',
                    fontSize: 14,
                    lineHeight: 1,
                  }}
                >
                  x
                </button>
              </div>
            )}
            <textarea
              ref={inputRef} value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={artifactContext
                ? `Ask Amadeus about ${artifactContext.title}…`
                : 'Type a message or press mic to speak...'}
              disabled={!connected} rows={3}
              className="w-full resize-none text-[12px] leading-[150%] placeholder-[var(--faint)] disabled:opacity-40"
              style={{ height: 64, fontFamily: 'var(--font-cjk)', color: 'var(--text)', backgroundColor: 'transparent', border: 'none', outline: 'none', padding: 0 }}
            />

            <div className="flex items-center gap-2" style={{ marginTop: 7 }}>
              <select value={provider} onChange={e => handleProviderChange(e.target.value)} className={comboCls} style={{ width: 116, height: 30 }}>
                {['local', 'deepseek', 'openai', 'gemini', 'bedrock', 'hybrid', 'hybrid2', 'hybrid3'].map(p => <option key={p} value={p}>{p}</option>)}
              </select>
              <div className="flex-1" />

              <button onClick={handleMicToggle}
                className="flex items-center justify-center shrink-0 cursor-pointer transition-colors" disabled={!connected}
                style={{
                  width: 36, height: 36, borderRadius: 18,
                  color: asrListening ? '#DC2626' : 'var(--muted)',
                  backgroundColor: asrListening ? '#FEF2F2' : 'transparent',
                  border: asrListening ? '1px solid #DC2626' : '1px solid var(--border)',
                  animation: asrListening ? 'pulse 1.5s ease-in-out infinite' : 'none',
                }}
                onMouseEnter={e => {
                  if (!asrListening) {
                    const b = e.currentTarget; b.style.color = 'var(--text)'; b.style.backgroundColor = 'var(--hover)'; b.style.borderColor = 'var(--border-strong)'
                  }
                }}
                onMouseLeave={e => {
                  if (!asrListening) {
                    const b = e.currentTarget; b.style.color = 'var(--muted)'; b.style.backgroundColor = 'transparent'; b.style.borderColor = 'var(--border)'
                  }
                }}
              >
                <FluentIcon name="Microphone" size={16} />
              </button>

              <button
                type="button"
                title={visualButtonTitle}
                aria-label={visualButtonTitle}
                disabled={visualButtonDisabled}
                onPointerDown={handleVisionPointerDown}
                onPointerUp={handleVisionPointerUp}
                onPointerLeave={handleVisionPointerCancel}
                onPointerCancel={handleVisionPointerCancel}
                onContextMenu={handleVisionContextMenu}
                className="flex items-center justify-center shrink-0 transition-colors"
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: 18,
                  color: visualButtonDisabled
                    ? 'var(--faint)'
                    : visionVideoMode
                      ? '#8B5CF6'
                      : pendingVisualAttachment
                        ? 'var(--accent)'
                        : 'var(--muted)',
                  backgroundColor: visualButtonDisabled
                    ? 'transparent'
                    : visionVideoMode
                      ? 'rgba(139, 92, 246, 0.12)'
                      : pendingVisualAttachment
                        ? 'rgba(0, 120, 212, 0.10)'
                        : 'transparent',
                  border: visualButtonDisabled
                    ? '1px solid var(--border)'
                    : visionVideoMode
                      ? '1px solid rgba(139, 92, 246, 0.65)'
                      : pendingVisualAttachment
                        ? '1px solid var(--accent)'
                        : '1px solid var(--border)',
                  cursor: visualButtonDisabled ? 'not-allowed' : 'pointer',
                  opacity: visualButtonDisabled ? 0.45 : 1,
                }}
                onMouseEnter={e => {
                  if (visualButtonDisabled || visionVideoMode || pendingVisualAttachment) return
                  const b = e.currentTarget
                  b.style.color = 'var(--text)'
                  b.style.backgroundColor = 'var(--hover)'
                  b.style.borderColor = 'var(--border-strong)'
                }}
                onMouseLeave={e => {
                  if (visionVideoMode || pendingVisualAttachment) return
                  const b = e.currentTarget
                  b.style.color = visualButtonDisabled ? 'var(--faint)' : 'var(--muted)'
                  b.style.backgroundColor = 'transparent'
                  b.style.borderColor = 'var(--border)'
                }}
              >
                <FluentIcon name={visionVideoMode ? 'Video' : 'Photo'} size={16} />
              </button>

              <input
                ref={imageInputRef}
                type="file"
                accept="image/*"
                style={{ display: 'none' }}
                onChange={handleVisionFileChange}
              />

              <button onClick={handleSend} disabled={!connected || (!input.trim() && !pendingVisualAttachment)}
                className="flex items-center justify-center shrink-0 cursor-pointer transition-colors"
                style={{
                  width: 38, height: 38, borderRadius: 19, color: '#FFFFFF',
                  backgroundColor: connected && (input.trim() || pendingVisualAttachment) ? 'var(--accent)' : 'var(--border-strong)',
                  border: '1px solid ' + (connected && (input.trim() || pendingVisualAttachment) ? 'var(--accent)' : 'var(--border-strong)'),
                }}
                onMouseEnter={e => { if (connected && (input.trim() || pendingVisualAttachment)) (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--accent-hover)' }}
                onMouseLeave={e => { if (connected && (input.trim() || pendingVisualAttachment)) (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--accent)' }}
              >
                <FluentIcon name="Send" size={18} color="#FFFFFF" />
              </button>
            </div>

            {visionWindowPickerOpen && (
              <div
                style={{
                  marginTop: 9,
                  borderRadius: 10,
                  border: '1px solid var(--border)',
                  backgroundColor: 'var(--surface)',
                  boxShadow: '0 8px 22px rgba(17,24,39,0.10)',
                  overflow: 'hidden',
                }}
              >
                <div
                  className="flex items-center gap-2"
                  style={{
                    padding: '8px 9px',
                    borderBottom: '1px solid var(--border)',
                    color: 'var(--muted)',
                    fontSize: 11,
                  }}
                >
                  <span className="font-[600]" style={{ color: 'var(--text)' }}>Monitor window</span>
                  <button
                    type="button"
                    onClick={() => loadVisionWindows().catch(() => {})}
                    disabled={visionWindowLoading}
                    className="ml-auto text-[11px]"
                    style={{
                      border: '1px solid var(--border)',
                      borderRadius: 7,
                      background: 'transparent',
                      color: 'var(--muted)',
                      cursor: visionWindowLoading ? 'default' : 'pointer',
                      padding: '3px 7px',
                    }}
                  >
                    Refresh
                  </button>
                  <button
                    type="button"
                    onClick={handleVisionFullScreenSelect}
                    className="text-[11px]"
                    style={{
                      border: '1px solid var(--border)',
                      borderRadius: 7,
                      background: 'transparent',
                      color: 'var(--muted)',
                      cursor: 'pointer',
                      padding: '3px 7px',
                    }}
                  >
                    Full screen
                  </button>
                  <button
                    type="button"
                    onClick={() => setVisionWindowPickerOpen(false)}
                    title="Close"
                    style={{
                      border: 'none',
                      background: 'transparent',
                      color: 'var(--faint)',
                      cursor: 'pointer',
                      fontSize: 15,
                      lineHeight: 1,
                    }}
                  >
                    x
                  </button>
                </div>
                <div className="chat-scroll-area" style={{ maxHeight: 184, overflowY: 'auto', padding: 5 }}>
                  {visionWindowLoading && (
                    <div style={{ padding: '10px 8px', color: 'var(--faint)', fontSize: 11 }}>
                      Loading windows...
                    </div>
                  )}
                  {!visionWindowLoading && visionWindowError && (
                    <div style={{ padding: '10px 8px', color: '#C42B1C', fontSize: 11 }}>
                      {visionWindowError}
                    </div>
                  )}
                  {!visionWindowLoading && !visionWindowError && visionWindows.length === 0 && (
                    <div style={{ padding: '10px 8px', color: 'var(--faint)', fontSize: 11 }}>
                      No visible windows found.
                    </div>
                  )}
                  {!visionWindowLoading && !visionWindowError && visionWindows.map(win => (
                    <button
                      key={win.hwnd}
                      type="button"
                      onClick={() => handleVisionWindowSelect(win)}
                      className="w-full flex items-center gap-2 text-left"
                      style={{
                        border: 'none',
                        borderRadius: 8,
                        background: win.selected ? 'var(--pressed)' : 'transparent',
                        color: 'var(--text)',
                        cursor: 'pointer',
                        padding: '7px 8px',
                        marginBottom: 2,
                      }}
                      onMouseEnter={e => {
                        if (!win.selected) e.currentTarget.style.backgroundColor = 'var(--hover)'
                      }}
                      onMouseLeave={e => {
                        if (!win.selected) e.currentTarget.style.backgroundColor = 'transparent'
                      }}
                    >
                      <span
                        className="truncate"
                        style={{ flex: 1, minWidth: 0, fontSize: 11, fontWeight: win.selected ? 600 : 500 }}
                      >
                        {win.title}
                      </span>
                      {win.processName && (
                        <span className="truncate" style={{ maxWidth: 110, color: 'var(--faint)', fontSize: 10 }}>
                          {win.processName}
                        </span>
                      )}
                      {win.rect && (
                        <span style={{ color: 'var(--faint)', fontSize: 10, whiteSpace: 'nowrap' }}>
                          {Math.round(win.rect.width)}x{Math.round(win.rect.height)}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <style>{`@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.6; } }`}</style>
      </div>
      </div>
    </div>
  )

  return (
    <div ref={splitRootRef} className="flex-1 flex min-h-0" style={{ backgroundColor: 'var(--bg)' }}>
      {/* Character render area (left, PixiJS mode only) */}
      {renderActive && (
        <div className="crt-render-stage flex flex-col min-w-0" style={{ minWidth: CHARACTER_MIN_W, flex: '1 1 auto', backgroundColor: 'var(--bg)' }}>
          {/* PixiJS render iframe */}
          <iframe
            ref={renderFrameRef}
            src={renderAssetUrl || 'about:blank'}
            title="PixiJS Render"
            onLoad={handleRenderFrameLoad}
            className="flex-1 border-0"
            style={{ width: '100%', backgroundColor: 'var(--bg)', pointerEvents: isSplitResizing ? 'none' : 'auto' }}
          />
          {CRT_WORK_WIDGET_DEMO_ENABLED && <CrtWorkWidget />}

          {/* Character tools bar (58px, light theme) */}
          <div
            className="flex items-center shrink-0"
            style={{
              height: 58, backgroundColor: 'var(--surface)',
              borderTop: '1px solid var(--border)',
              padding: '0 14px', gap: 10,
            }}
          >
            <button title="Switch to VTS render"
              style={toolBtnStyle(42)}
              onClick={() => window.dispatchEvent(new CustomEvent('navigate', { detail: 'toggle-render' }))}
              onMouseEnter={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--surface-alt)' }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent' }}
            >
              <FluentIcon name="Movie" size={18} />
            </button>
            <div className="flex-1" />
            <button title={`Current: ${pixiSubmodeLabel}. Click to switch.`}
              style={toolBtnStyle(42)}
              onClick={handlePixiSubmodeCycle}
              onMouseEnter={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--surface-alt)' }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent' }}
            >
              <FluentIcon name="Album" size={18} />
            </button>
            <button title="Open expression presets"
              style={toolBtnStyle(42)}
              onClick={() => window.dispatchEvent(new CustomEvent('navigate', { detail: 'expressions' }))}
              onMouseEnter={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--surface-alt)' }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent' }}
            >
              <FluentIcon name="Palette" size={18} />
            </button>
            <button title="Open transparent overlay"
              style={toolBtnStyle(42)}
              onClick={() => send('expression.set_backend', { backend: 'graph', overlay: true }).catch(() => {})}
              onMouseEnter={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--surface-alt)' }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent' }}
            >
              <FluentIcon name="Pin" size={18} />
            </button>
          </div>
        </div>
      )}

      {renderActive && (
        <div
          className={`render-chat-splitter ${isSplitResizing ? 'active' : ''}`}
          role="separator"
          aria-orientation="vertical"
          title="Resize render and chat panels"
          onPointerDown={handleSplitPointerDown}
        />
      )}

      {/* Chat panel (right) */}
      {chatPanel}
    </div>
  )
}
