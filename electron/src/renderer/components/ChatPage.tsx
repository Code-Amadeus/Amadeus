import { prepareImageAttachment } from '../../../../render/web/chat_image_attachment.mjs'
import { useState, useCallback, useEffect, useRef } from 'react'
import { Fragment, useMemo } from 'react'
import type {
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
} from 'react'
import ChatBubble from './ChatBubble'
import { markRuntimeSettingsApplied, persistDesktopRuntimeSettings } from './desktopRuntimeSettings'
import ChatSessionRail, {
  type ChatProjectSummary,
  type ChatSessionSummary,
} from './ChatSessionRail'
import ChatWorkActivityCard from './ChatWorkActivityCard'
import FluentIcon from './FluentIcon'
import ProjectAppsPanel, { type ProjectAppSummary } from './ProjectAppsPanel'
import { SettingsButton } from './SettingsPrimitives'
import type { WorkFileSummary } from './WorkFilesPanel'
import CrtWorkWidget from './work/CrtWorkWidget'
import { attentionRequestsFromEnvelope } from './work/attentionProjection'
import type { AttentionRequest } from './work/types'
import {
  activitiesFromProviderRuns,
  applyProviderEvent,
  applyProviderResult,
  type ChatWorkActivityRun,
} from './chatWorkActivity'
import {
  INTERRUPTED_MARKER,
  acceptedRoleMessage,
  automaticSessionSelection,
  applyRoleMessage,
  assistantTurnAnchors,
  finishAssistantTurn,
  updateAssistantMessage,
  chatAsrDestination,
  chatEventMatchesSession,
  patchInterruptedMessage,
  runSessionSelection,
  type Message,
} from './chatMessageState'
import {
  chatTranslationCandidates,
  chatTranslationKey,
} from './chatTranslationState'
import { useI18n } from '../i18n'
import { startupCharacterSelection } from '../../shared/characterStartup'
import { characterCatalog, type CharacterRecord } from './characterManagement'
import { visualStatusFromFrame, type VisualSurfaceStatus } from './characterVisuals'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
  renderActive: boolean       // visibility of the embedded character surface
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
const VISION_LONG_PRESS_MS = 560
const DRAFT_APPS_VIEW_ID = '__draft_apps__'
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
  'render.character_config',
  'render.character_intent',
  'render.character_release',
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


export default function ChatPage({ send, subscribe, connected, renderActive, renderAssetUrl }: Props) {
  const { t } = useI18n()
  const [messages, setMessages] = useState<Message[]>([])
  const [chatTranslationEnabled, setChatTranslationEnabled] = useState(false)
  const [chatTranslationLanguage, setChatTranslationLanguage] = useState('zh-CN')
  const [chatTranslations, setChatTranslations] = useState<Record<string, string>>({})
  const [workActivities, setWorkActivities] = useState<ChatWorkActivityRun[]>([])
  const [attentionRequests, setAttentionRequests] = useState<AttentionRequest[]>([])
  const [attentionResolving, setAttentionResolving] = useState('')
  const [attentionError, setAttentionError] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [input, setInput] = useState('')
  const [provider, setProvider] = useState('deepseek')
  const [canUseMultimodal, setCanUseMultimodal] = useState(false)
  const [chatAvatars, setChatAvatars] = useState({ user: '', assistant: '' })
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([])
  const [projects, setProjects] = useState<ChatProjectSummary[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [currentCharacterId, setCurrentCharacterId] = useState('')
  const [sessionCharacters, setSessionCharacters] = useState<CharacterRecord[] | null>(null)
  const [startupCharacterLocked, setStartupCharacterLocked] = useState(false)
  const [sessionReady, setSessionReady] = useState(false)
  const [sessionSwitching, setSessionSwitching] = useState(false)
  const [sessionSelectionNotice, setSessionSelectionNotice] = useState('')
  const sessionSelectionRef = useRef({ pending: 0 })
  const [projectCorrectionOpen, setProjectCorrectionOpen] = useState(false)
  const [projectViewId, setProjectViewId] = useState('')
  const [projectApps, setProjectApps] = useState<ProjectAppSummary[]>([])
  const [projectFiles, setProjectFiles] = useState<WorkFileSummary[]>([])
  const artifactLoadRevision = useRef(0)
  const [projectFilesComplete, setProjectFilesComplete] = useState(true)
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
  const renderStatusRef = useRef<VisualSurfaceStatus | null>(null)
  const renderFrameLoadedUrlRef = useRef('')
  const renderConnectionRef = useRef(connected)
  const interruptedTurnIdsRef = useRef<Set<string>>(new Set())
  const activeStreamTurnIdRef = useRef('')
  const streamingTextRef = useRef('')
  const lastAssistantTurnIdRef = useRef('')
  const chatTranslationRequestedRef = useRef<Set<string>>(new Set())
  const chatTranslationGenerationRef = useRef(0)
  const activeSessionRef = useRef('')
  const visionPressTimerRef = useRef<number | null>(null)
  const visionLongPressRef = useRef(false)
  const toMessages = useCallback((items: unknown): Message[] => {
    if (!Array.isArray(items)) return []
    return items
      .map((m: any) => ({
        role: (m.role === 'assistant' || m.role === 'system') ? m.role : 'user',
        text: String(m.content ?? m.text ?? ''),
        turnId: String(m.turn_id ?? m.turnId ?? '') || undefined,
        messageId: String(m.message_id ?? m.messageId ?? '') || undefined,
        streaming: false,
      }))
      .filter(m => m.text)
  }, [])

  const upsertAssistantMessage = useCallback((turnId: string, text: string, streamingValue: boolean) => {
    setMessages(prev => updateAssistantMessage(prev, turnId, text, streamingValue))
  }, [])

  const upsertUserMessage = useCallback((turnId: string, text: string) => {
    setMessages(prev => {
      if (turnId && prev.some(m => m.role === 'user' && m.turnId === turnId)) return prev
      return [...prev, { role: 'user', text, turnId: turnId || undefined }]
    })
  }, [])

  const refreshSessions = useCallback(async () => {
    const res = await send('session.list', {})
    const list = Array.isArray(res.sessions) ? res.sessions as unknown as ChatSessionSummary[] : []
    setSessions(list)
    setCurrentCharacterId(typeof res.current_character_id === 'string' ? res.current_character_id : '')
    if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
    return {
      list,
      currentSessionId: String(res.current_session_id || ''),
      currentCharacterId: res.current_character_id,
    }
  }, [send])

  const applySessionPayload = useCallback((res: Record<string, unknown>) => {
    if (Array.isArray(res.sessions)) setSessions(res.sessions as unknown as ChatSessionSummary[])
    if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
    const session = res.session as ChatSessionSummary | undefined
    const sessionId = String(res.current_session_id || session?.id || '')
    if (sessionId || 'current_session_id' in res) {
      if (activeSessionRef.current !== sessionId) {
        setWorkActivities([])
      }
      activeSessionRef.current = sessionId
      setActiveSession(sessionId || null)
      if (!sessionId) {
        activeStreamTurnIdRef.current = ''
        lastAssistantTurnIdRef.current = ''
        interruptedTurnIdsRef.current.clear()
        setAttentionRequests([])
        setAttentionResolving('')
        setAttentionError('')
        setArtifactContext(null)
      }
    }
    if (Array.isArray(res.messages) || ('current_session_id' in res && !sessionId)) {
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

  const selectSession = useCallback((
    method: string, params: Record<string, unknown> = {}, fallbackNotice?: string,
  ) => (
    runSessionSelection(sessionSelectionRef.current, setSessionSwitching,
      () => send(method, params), applySessionPayload, setSessionSelectionNotice, fallbackNotice)
  ), [send, applySessionPayload])

  const loadSession = useCallback(async (id: string, closeArtifactView = false) => {
    try {
      const res = await selectSession('session.load', { session_id: id })
      if (res.ok === false) return
      if (closeArtifactView) setProjectViewId('')
      const session = res.session as ChatSessionSummary | undefined
      const sessionId = String(res.current_session_id || session?.id || id)
      await hydrateWorkActivities(sessionId)
    } catch {
      // Selection feedback is separate from the current conversation/history.
    }
  }, [selectSession, hydrateWorkActivities])

  const restoreOwnedSession = useCallback(async (
    list: ChatSessionSummary[], currentCharacterId: unknown, currentSessionId = '',
  ) => {
    const selected = automaticSessionSelection(list, currentCharacterId, currentSessionId)
    if (selected) await loadSession(selected.id)
    else await selectSession('session.create', {})
  }, [loadSession, selectSession])

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
      if (!cancelled) {
        setChatTranslationEnabled(enabled)
        setChatTranslationLanguage(String(values.subtitle_translation_language || 'zh-CN'))
      }
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
    if (!connected || !activeSession) {
      setAttentionRequests([])
      return
    }
    let cancelled = false
    const applyAttention = (payload: Record<string, unknown>) => {
      const sessionId = String(payload.sessionId || payload.session_id || '')
      if (!cancelled && (!sessionId || sessionId === activeSessionRef.current)) {
        setAttentionRequests(attentionRequestsFromEnvelope(payload))
      }
    }
    const unsubscribe = subscribe('attention.updated', applyAttention)
    void send('attention.list', {}).then(applyAttention).catch(() => {
      if (!cancelled) setAttentionRequests([])
    })
    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [activeSession, connected, send, subscribe])

  const resolveAttention = useCallback(async (requestId: string, optionId: string) => {
    if (!connected || attentionResolving) return
    setAttentionResolving(optionId)
    setAttentionError('')
    try {
      const response = await send('attention.resolve', {
        request_id: requestId,
        option_id: optionId,
      })
      if (response.ok === false) {
        throw new Error(String(response.detail || response.error || 'Scope choice was rejected'))
      }
      setAttentionRequests(attentionRequestsFromEnvelope(response))
    } catch (error) {
      setAttentionError(error instanceof Error ? error.message : String(error))
    } finally {
      setAttentionResolving('')
    }
  }, [attentionResolving, connected, send])

  useEffect(() => {
    chatTranslationGenerationRef.current += 1
    chatTranslationRequestedRef.current.clear()
    setChatTranslations({})
  }, [chatTranslationEnabled, chatTranslationLanguage])

  useEffect(() => {
    if (!chatTranslationEnabled) return

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
  }, [chatTranslationEnabled, chatTranslationLanguage, messages, send])

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

  const handleCharacterStatus = useCallback((event: MessageEvent) => {
    const status = visualStatusFromFrame(event.source, renderFrameRef.current?.contentWindow,
      event.data, 'amadeus.character.status')
    if (!status) return
    renderStatusRef.current = status
    send('visual.status', { ...status, surface: 'render' }).catch(error => {
      console.error('[render-bridge] character status report failed', error)
    })
  }, [send])

  useEffect(() => {
    if (!renderActive) return
    window.addEventListener('message', handleCharacterStatus)
    return () => {
      window.removeEventListener('message', handleCharacterStatus)
      const status = renderStatusRef.current
      renderStatusRef.current = null
      if (status && status.state !== 'unloaded') {
        void send('visual.status', { ...status, state: 'unloaded', surface: 'render' }).catch(error => {
          console.error('[render-bridge] unload status report failed', error)
        })
      }
    }
  }, [handleCharacterStatus, renderActive, send])

  const handleRenderFrameLoad = useCallback(() => {
    if (!renderActive || !renderAssetUrl || !renderFrameRef.current) return
    renderFrameLoadedUrlRef.current = renderAssetUrl
    if (!connected) return
    send('render.ready', {}).catch(error => {
      console.error('[render-bridge] state replay failed', error)
    })
  }, [connected, renderActive, renderAssetUrl, send])

  useEffect(() => {
    const wasConnected = renderConnectionRef.current
    renderConnectionRef.current = connected
    if (!renderActive || !renderAssetUrl || !renderFrameRef.current) {
      renderFrameLoadedUrlRef.current = ''
      return
    }
    if (!connected) {
      if (wasConnected) {
        postRenderEvent('render.speaking', { speaking: false })
        postRenderEvent('render.mouth', { value: 0 })
      }
      return
    }
    if (!wasConnected && renderFrameLoadedUrlRef.current === renderAssetUrl) {
      send('render.ready', {}).catch(error => {
        console.error('[render-bridge] reconnect state replay failed', error)
      })
    }
  }, [connected, postRenderEvent, renderActive, renderAssetUrl, send])

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

  // ChatPage remounts when returning from Settings; each live connection reads
  // fresh names and the desktop-owned launch source without changing ownership.
  useEffect(() => {
    if (!connected) return
    let current = true
    setSessionCharacters(null)
    setStartupCharacterLocked(false)
    void send('character.list').then(response => {
      if (current) setSessionCharacters(characterCatalog(response).characters)
    }).catch(() => {})
    void window.amadeus?.getDesktopSettings().then(snapshot => {
      if (!current || !snapshot) return
      const selection = startupCharacterSelection(snapshot)
      setStartupCharacterLocked(selection.locked)
    }).catch(() => {})
    return () => { current = false }
  }, [connected, send])

  // restore legacy persisted sessions
  useEffect(() => {
    if (!connected) {
      setSessionReady(false)
      return
    }
    setSessionReady(false)
    let cancelled = false
    ;(async () => {
      try {
        const { list, currentSessionId, currentCharacterId } = await refreshSessions()
        if (cancelled) return
        await restoreOwnedSession(list, currentCharacterId, currentSessionId)
      } catch (error) {
        if (!cancelled) setSessionSelectionNotice(error instanceof Error
          ? error.message : 'Could not switch chats.')
      } finally {
        if (!cancelled) setSessionReady(true)
      }
    })()
    return () => { cancelled = true }
  }, [connected, refreshSessions, restoreOwnedSession])

  // load initial config + subscribe to config changes (cross-page sync)
  useEffect(() => {
    if (!connected) return
    send('system.get_config', {}).then(res => {
      if (res?.llm_provider) setProvider(String(res.llm_provider))
      setCanUseMultimodal(res?.chat_supports_images === true)
      // The selected mode survives disabling vision; it is not the enabled state.
      setVisionVideoMode(res?.vision_enabled === true && res?.vision_mode === 'watching')
    }).catch(() => {})

    const unsub = subscribe('system.config', (p) => {
      const values = (p.values ?? p) as Record<string, unknown>
      if (values.llm_provider !== undefined) setProvider(String(values.llm_provider))
      if (values.chat_supports_images !== undefined) setCanUseMultimodal(values.chat_supports_images === true)
      setVisionVideoMode(values.vision_enabled === true && values.vision_mode === 'watching')
    })
    return unsub
  }, [subscribe, send, connected])

  useEffect(() => {
    if (!canUseMultimodal) {
      setPendingVisualAttachment(null)
      setVisionWindowPickerOpen(false)
      setVisionVideoMode(prev => {
        if (prev) {
          const values = { vision_enabled: false }
          persistDesktopRuntimeSettings(values)
            .then(async saved => {
              await send('system.set_config', { values })
              await markRuntimeSettingsApplied(values, saved.pendingRevisions)
            })
            .catch(() => {})
        }
        return false
      })
    }
  }, [canUseMultimodal, send])

  // subscribe to backend events
  useEffect(() => {
    const unsubs: Array<() => void> = []
    unsubs.push(subscribe('chat.user', (p) => {
      const sessionId = String(p.session_id ?? '')
      if (sessionId && activeSessionRef.current && sessionId !== activeSessionRef.current) return
      const text = String(p.text ?? '').trim()
      if (!text) return
      upsertUserMessage(String(p.turn_id ?? ''), text)
    }))
    unsubs.push(subscribe('chat.token', (p) => {
      if (!chatEventMatchesSession(p, activeSessionRef.current, interruptedTurnIdsRef.current)) return
      const turnId = String(p.turn_id ?? '')
      if (turnId) activeStreamTurnIdRef.current = turnId
      streamingTextRef.current = String(p.token ?? '')
      setStreaming(true)
      setStreamingText(streamingTextRef.current)
      upsertAssistantMessage(turnId, streamingTextRef.current, true)
    }))
    unsubs.push(subscribe('chat.complete', (p) => {
      if (!chatEventMatchesSession(p, activeSessionRef.current, interruptedTurnIdsRef.current)) return
      const text = String(p.full_text ?? p.token ?? '')
      const turnId = String(p.turn_id ?? '')
      setMessages(prev => finishAssistantTurn(prev, turnId, text))
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
    unsubs.push(subscribe('chat.role_message', (p) => {
      const line = acceptedRoleMessage(p, activeSessionRef.current, interruptedTurnIdsRef.current)
      if (line) setMessages(prev => applyRoleMessage(prev, line))
      // Client acceptance is separate from the current foreground stream and
      // never dispatches work or claims physical audio/render completion.
      send('chat.role_received', {
        session_id: p.session_id, message_id: p.message_id, accepted: line !== null,
      }).catch(() => {})
    }))
    unsubs.push(subscribe('chat.interrupted', (p) => {
      console.info('[ChatPage] chat.interrupted', p)
      if (String(p.session_id ?? '') !== activeSessionRef.current) return
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
      if (!chatEventMatchesSession(p, activeSessionRef.current, interruptedTurnIdsRef.current)) return
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
        const destination = chatAsrDestination(p.source)
        if (destination === 'ignore') return
        if (destination === 'direct') {
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
  }, [subscribe, refreshSessions, upsertAssistantMessage, upsertUserMessage, applySessionPayload, hydrateWorkActivities])

  const handleSend = useCallback(async () => {
    const typedText = input.trim()
    const text = typedText || (pendingVisualAttachment ? '请看这张图片。' : '')
    if (!text || !connected || !sessionReady || sessionSelectionRef.current.pending > 0) return
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
    let sessionId = activeSessionRef.current
    if (!sessionId) {
      try {
        const res = await selectSession('session.create', {})
        const session = res.session as ChatSessionSummary | undefined
        if (session) {
          sessionId = session.id
        }
      } catch {
        // Send can still proceed without persistence if the backend is mid-reconnect.
      }
    }
    setInput('')
    setStreamingText('')
    streamingTextRef.current = ''
    setStreaming(false)
    try {
      const turnId = crypto.randomUUID()
      interruptedTurnIdsRef.current.delete(turnId)
      activeStreamTurnIdRef.current = turnId
      await send('chat.send', {
        text,
        provider,
        turn_id: turnId,
        session_id: sessionId || '',
        ...(visual ? { visual } : {}),
      })
      if (pendingVisualAttachment) setPendingVisualAttachment(null)
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Send failed: backend unreachable' }])
    }
  }, [input, connected, sessionReady, canUseMultimodal, pendingVisualAttachment, send, provider, selectSession])

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
      const values = next
        ? { vision_enabled: true, vision_mode: 'watching', vision_scope: 'full_screen' }
        : { vision_enabled: false }
      const saved = await persistDesktopRuntimeSettings(values)
      await send('system.set_config', { values })
      await markRuntimeSettingsApplied(values, saved.pendingRevisions)
    } catch {
      setVisionVideoMode(!next)
    }
  }, [connected, canUseMultimodal, visionVideoMode, send])

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
      const values = {
          vision_enabled: true,
          vision_mode: visionVideoMode ? 'watching' : 'on_demand',
          vision_scope: 'selected_window',
          vision_window_handle: windowItem.hwnd,
      }
      const saved = await persistDesktopRuntimeSettings(values)
      await send('system.set_config', { values })
      await markRuntimeSettingsApplied(values, saved.pendingRevisions)
      setVisionWindows(prev => prev.map(item => ({ ...item, selected: item.hwnd === windowItem.hwnd })))
      setVisionWindowPickerOpen(false)
      setPendingVisualAttachment(null)
    } catch (error) {
      setVisionWindowError(error instanceof Error ? error.message : 'Could not switch window')
    }
  }, [connected, canUseMultimodal, send, visionVideoMode])

  const handleVisionFullScreenSelect = useCallback(async () => {
    if (!connected || !canUseMultimodal) return
    setVisionWindowError('')
    try {
      const values = {
          vision_enabled: true,
          vision_mode: visionVideoMode ? 'watching' : 'on_demand',
          vision_scope: 'full_screen',
          vision_window_handle: '',
      }
      const saved = await persistDesktopRuntimeSettings(values)
      await send('system.set_config', { values })
      await markRuntimeSettingsApplied(values, saved.pendingRevisions)
      setVisionWindows(prev => prev.map(item => ({ ...item, selected: false })))
      setVisionWindowPickerOpen(false)
    } catch (error) {
      setVisionWindowError(error instanceof Error ? error.message : 'Could not switch to full screen')
    }
  }, [connected, canUseMultimodal, send, visionVideoMode])

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
    let persisted = false
    try {
      const saved = await persistDesktopRuntimeSettings({ llm_provider: v })
      persisted = saved.persisted
      const res = await send('system.set_config', { values: { llm_provider: v } })
      await markRuntimeSettingsApplied({ llm_provider: v }, saved.pendingRevisions)
      const values = res.values as Record<string, unknown> | undefined
      if (values?.llm_provider) setProvider(String(values.llm_provider))
      if (values?.chat_supports_images !== undefined) setCanUseMultimodal(values.chat_supports_images === true)
    } catch {
      setMessages(prev => [...prev, {
        role: 'system',
        text: t(persisted
          ? 'Default model saved for the next backend start; the current conversation still uses the previous model.'
          : 'Model switch failed; wait for the current reply to finish and try again.'),
      }])
    }
  }, [send, t])

  const handleNewSession = useCallback(async () => {
    try {
      await selectSession('session.create', {})
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Could not create session' }])
    }
  }, [selectSession])

  const handleNewProjectSession = useCallback(async (projectId: string) => {
    const project = projects.find(candidate => candidate.projectId === projectId)
    try {
      const res = await selectSession('session.create', {
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
    } catch {
      setMessages(prev => [...prev, { role: 'system', text: 'Could not create the Project chat' }])
    }
  }, [projects, selectSession])

  const handleNewProject = useCallback(async () => {
    try {
      const selection = await window.amadeus?.selectProjectDirectory()
      if (!selection || selection.cancelled) return
      if (!selection.ok || !selection.path) {
        throw new Error(selection.detail || 'Could not select a Project directory')
      }
      const res = await selectSession('project.create', { workspace_path: selection.path })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not create the Project'))
      }
    } catch (error) {
      setMessages(prev => [...prev, {
        role: 'system',
        text: error instanceof Error ? error.message : String(error),
      }])
    }
  }, [selectSession])

  const loadArtifactCatalog = useCallback(async (projectId: string) => {
    const revision = ++artifactLoadRevision.current
    setProjectAppsLoading(true)
    setProjectAppsFeedback('')
    try {
      const res = await send('work.artifacts.list', projectId
        ? { scope: 'project', project_id: projectId, limit: 100 }
        : { scope: 'drafts', limit: 5 })
      if (revision !== artifactLoadRevision.current) return
      if (res.ok === false) throw new Error(String(res.message || res.error || 'Could not load artifacts'))
      setProjectApps(Array.isArray(res.apps) ? res.apps as unknown as ProjectAppSummary[] : [])
      setProjectFiles(Array.isArray(res.files) ? res.files as unknown as WorkFileSummary[] : [])
      setProjectAppsComplete(res.appsComplete !== false)
      setProjectFilesComplete(res.filesComplete !== false)
    } catch (error) {
      if (revision !== artifactLoadRevision.current) return
      setProjectApps([])
      setProjectFiles([])
      setProjectAppsComplete(true)
      setProjectFilesComplete(true)
      setProjectAppsFeedback(error instanceof Error ? error.message : String(error))
    } finally {
      if (revision === artifactLoadRevision.current) setProjectAppsLoading(false)
    }
  }, [send])
  const loadProjectApps = useCallback((projectId: string) => loadArtifactCatalog(projectId), [loadArtifactCatalog])
  const loadDraftApps = useCallback(() => loadArtifactCatalog(''), [loadArtifactCatalog])
  const readArtifactFile = useCallback((file: WorkFileSummary) => send('work.artifact.read', {
    work_item_id: file.workItemId, artifact_id: file.artifactId,
  }), [send])

  const handleOpenProject = useCallback((projectId: string) => {
    setProjectViewId(projectId)
    setProjectFiles([])
    setProjectApps([])
    setProjectAppsFeedback('')
    void loadProjectApps(projectId)
  }, [loadProjectApps])

  const handleOpenDraftApps = useCallback(() => {
    setProjectViewId(DRAFT_APPS_VIEW_ID)
    setProjectFiles([])
    setProjectApps([])
    setProjectAppsFeedback('')
    void loadDraftApps()
  }, [loadDraftApps])

  const openArtifactConversation = useCallback(async (
    projectId: string,
    app: ProjectAppSummary,
  ) => {
    const res = await selectSession('session.open_context', {
      project_id: app.projectId || projectId,
      work_item_id: app.workItemId,
    })
    if (res.ok === false) {
      throw new Error(String(res.message || res.error || 'Could not open the Artifact conversation'))
    }
  }, [selectSession])

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
        const source = await selectSession('session.load', { session_id: app.sourceSessionId })
        if (source.ok !== false) {
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
  }, [selectSession, loadProjectApps, openArtifactConversation, projectAppAction, projectViewId, refreshSessions, send, streaming])

  const correctProjectBinding = useCallback(async (projectId: string) => {
    setProjectCorrectionOpen(false)
    if (!activeSession) return
    const currentProjectId = String(
      sessions.find(session => session.id === activeSession)?.context?.projectId || '',
    )
    if (projectId === currentProjectId) return
    try {
      const res = await selectSession('session.correct_project', {
        session_id: activeSession,
        project_id: projectId,
      })
      if (res.ok === false) {
        throw new Error(String(res.message || res.error || 'Could not move the chat'))
      }
    } catch (error) {
      setMessages(prev => [...prev, {
        role: 'system',
        text: error instanceof Error ? error.message : String(error),
      }])
    }
  }, [activeSession, selectSession, sessions])

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
      const res = await selectSession('session.delete', { session_id: id }, 'Could not delete this chat.')
      if (res.ok === false) return
      if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
      const next = Array.isArray(res.sessions) ? res.sessions as unknown as ChatSessionSummary[] : sessions.filter(s => s.id !== id)
      setSessions(next)
      if (activeSession === id) {
        const selected = automaticSessionSelection(next, res.current_character_id, String(res.current_session_id || ''))
        if (selected) await loadSession(selected.id)
      }
    } catch (error) {
      setSessionSelectionNotice(error instanceof Error && error.message.trim()
        ? error.message : 'Could not delete this chat.')
    }
  }, [activeSession, loadSession, selectSession, sessions])

  // The rail edits titles inline; Electron does not implement window.prompt.
  const handleRenameSession = useCallback(async (id: string, nextTitle: string) => {
    const title = nextTitle.trim()
    if (!title) return
    try {
      await runSessionSelection({ pending: 0 }, () => {},
        () => send('session.rename', { session_id: id, title }),
        res => {
          if (Array.isArray(res.sessions)) setSessions(res.sessions as unknown as ChatSessionSummary[])
          if (Array.isArray(res.projects)) setProjects(res.projects as unknown as ChatProjectSummary[])
        }, setSessionSelectionNotice, 'Could not rename this chat.')
    } catch {
      // Mutation feedback is separate from the current conversation/history.
    }
  }, [send])

  const handleSplitPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!renderActive) return
    event.preventDefault()
    setIsSplitResizing(true)
  }, [renderActive])

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
    ? t('Select an image-capable model, such as DeepSeek Flash, OpenAI, or Gemini')
    : visionVideoMode
      ? t('Global vision watching is on. Hold to turn it off. Right-click to choose a window.')
      : pendingVisualAttachment
        ? t('{name} will be sent with the next message. Click to choose another image; hold for watching; right-click for window.', { name: pendingVisualAttachment.name })
        : t('Choose an image for the next message. Hold for watching; right-click for window.')

  const activitiesByTurn = useMemo(() => {
    const grouped = new Map<string, ChatWorkActivityRun[]>()
    for (const activity of workActivities) {
      const current = grouped.get(activity.turnId) || []
      current.push(activity)
      grouped.set(activity.turnId, current)
    }
    return grouped
  }, [workActivities])
  const activityAnchors = useMemo(() => assistantTurnAnchors(messages), [messages])

  /* Chat panel (right side) */
  const chatPanel = (
    <div
      className="chat-panel"
      data-split={renderActive || undefined}
      style={{
        minWidth: renderActive ? CHAT_PANEL_MIN_W : 0,
        width: renderActive ? chatWidth : '100%',
        flex: renderActive ? undefined : 1,
      }}
      >
      <div className="chat-frame">
      <ChatSessionRail
        sessions={sessions}
        projects={projects}
        activeId={activeSession}
        currentCharacterId={currentCharacterId}
        characters={sessionCharacters}
        connected={connected}
        startupCharacterLocked={startupCharacterLocked}
        onNotice={setSessionSelectionNotice}
        artifactViewId={projectViewId === DRAFT_APPS_VIEW_ID ? 'drafts' : projectViewId}
        onSelect={id => { void loadSession(id, true) }}
        onNew={() => { setProjectViewId(''); void handleNewSession() }}
        onNewProject={() => { void handleNewProject() }}
        onNewProjectSession={id => { void handleNewProjectSession(id) }}
        onOpenProject={handleOpenProject}
        onOpenDraftApps={handleOpenDraftApps}
        onRename={handleRenameSession}
        onDelete={id => { void handleDeleteSession(id) }}
      />

      <div className="flex flex-col min-w-0 flex-1 h-full relative">
      {sessionSelectionNotice && (
        <div
          role="alert"
          aria-label={t('Chat selection notice')}
          className="chat-notice"
        >
          <span className="flex-1 min-w-0">{t(sessionSelectionNotice)}</span>
          <button
            type="button"
            aria-label={t('Dismiss chat selection notice')}
            onClick={() => setSessionSelectionNotice('')}
            className="icon-button" data-size="small" data-tone="faint"
          >
            <FluentIcon name="Dismiss" size={14} />
          </button>
        </div>
      )}
      {projectView && (
        <ProjectAppsPanel
          key={projectViewId}
          files={projectFiles}
          readFile={readArtifactFile}
          project={projectView}
          scope={projectViewId === DRAFT_APPS_VIEW_ID ? 'drafts' : 'project'}
          apps={projectApps}
          appsComplete={projectAppsComplete}
          filesComplete={projectFilesComplete}
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
      <div className="chat-bar">
        <span className="chat-bar-title">
          {activeSession ? sessions.find(s => s.id === activeSession)?.title ?? t('Chat') : t('Chat')}
        </span>
        {projectCorrectionOpen ? (
          <select
            autoFocus
            aria-label={t('Move chat to Project')}
            defaultValue=""
            onBlur={() => setProjectCorrectionOpen(false)}
            onChange={event => {
              const value = event.target.value
              if (value) void correctProjectBinding(value === '__draft__' ? '' : value)
            }}
            disabled={!connected || streaming}
            className="chat-select chat-select-project"
          >
            <option value="" disabled>{t('Move chat…')}</option>
            <option value="__draft__">{t('Draft')}</option>
            {projects.map(project => (
              <option key={project.projectId} value={project.projectId}>{project.name}</option>
            ))}
          </select>
        ) : (
          <div
            className="chat-project-chip"
            aria-label={t('Current chat project')}
            title={activeContext?.projectId
              ? t('This chat is bound to {name}', { name: activeContext.projectName })
              : t('This is a default Draft chat')}
          >
            <span className="chat-project-name">
              {activeContext?.projectId ? activeContext.projectName : t('Draft')}
            </span>
            {!activeContext?.projectId && activeContext?.canPromoteToProject && (
              <button
                type="button"
                onClick={() => { void promoteActiveDraft() }}
                title={t('Promote this Draft to a Project')}
                className="chat-project-action"
                data-tone="accent"
              >
                {t('Promote')}
              </button>
            )}
            {activeSession && (
              <button
                type="button"
                onClick={() => setProjectCorrectionOpen(true)}
                title={t('Move this chat and preserve its history. Existing Work keeps its original Project; moving waits for active Work to finish.')}
                className="chat-project-action"
              >
                {t('Move')}
              </button>
            )}
          </div>
        )}
        <button
          type="button"
          onClick={() => { void handleNewSession() }}
          title={t('New chat')}
          aria-label={t('New chat')}
          className="icon-button"
        >
          <FluentIcon name="Edit" size={16} />
        </button>
      </div>

      {/* Chat messages */}
      <div className="chat-scroll-area chat-messages">
        <div className="chat-message-list">
          {messages.length === 0 && !streaming && (
            <p className="chat-empty">{t('Type a message to start.')}</p>
          )}
          {messages.map((msg, i) => {
            const key = msg.messageId || `${msg.role}-${msg.turnId || 'local'}-${i}`
            const attached = msg.role === 'assistant' && msg.turnId && activityAnchors.get(msg.turnId) === i
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
                  hasPreviousMessage={i > 0}
                />
                {attached.map(activity => (
                  <ChatWorkActivityCard
                    key={activity.runId}
                    activity={activity}
                  />
                ))}
              </Fragment>
            )
          })}
          {attentionRequests[0] && (
            <div
              role="dialog"
              aria-label={attentionRequests[0].title}
              className="chat-attention"
            >
              <strong className="chat-attention-title">{attentionRequests[0].title}</strong>
              {attentionRequests[0].prompt && (
                <p className="chat-attention-prompt">
                  {attentionRequests[0].prompt}
                </p>
              )}
              <div className="chat-attention-options">
                {attentionRequests[0].options.map(option => (
                  <SettingsButton
                    key={option.id}
                    disabled={Boolean(attentionResolving)}
                    onClick={() => { void resolveAttention(attentionRequests[0].id, option.id) }}
                    title={option.description}
                  >
                    {option.label}{attentionResolving === option.id ? '…' : ''}
                  </SettingsButton>
                ))}
              </div>
              {attentionError && (
                <p className="chat-attention-error">{attentionError}</p>
              )}
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input bar */}
      <div className="chat-composer-bar">
        <div className="chat-composer">
          <div className="chat-composer-body">
            {artifactContext && (
              <div className="chat-composer-chip" data-tone="accent">
                <FluentIcon name="Chat" size={14} className="chat-accent" />
                <span className="truncate">
                  {t('Interacting with Amadeus about')} <strong>{artifactContext.title}</strong>
                </span>
                <button
                  type="button"
                  onClick={() => setArtifactContext(null)}
                  title={t('Dismiss Artifact context label')}
                  aria-label={t('Dismiss Artifact context label')}
                  className="icon-button ml-auto" data-size="small" data-tone="faint"
                >
                  <FluentIcon name="Dismiss" size={14} />
                </button>
              </div>
            )}
            {pendingVisualAttachment && (
              <div className="chat-composer-chip">
                <FluentIcon name="Photo" size={14} />
                <span className="chat-composer-chip-name">
                  {pendingVisualAttachment.name}
                </span>
                <span className="chat-faint shrink-0">
                  {pendingVisualAttachment.width}x{pendingVisualAttachment.height}
                </span>
                <button
                  type="button"
                  title={t('Remove image')}
                  aria-label={t('Remove image')}
                  onClick={() => setPendingVisualAttachment(null)}
                  className="icon-button ml-auto" data-size="small" data-tone="faint"
                >
                  <FluentIcon name="Dismiss" size={14} />
                </button>
              </div>
            )}
            <textarea
              ref={inputRef} value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={artifactContext
                ? t('Ask Amadeus about {title}…', { title: artifactContext.title })
                : t('Type a message or press mic to speak...')}
              disabled={!connected || !sessionReady || sessionSwitching} rows={3}
              className="chat-composer-input"
            />

            <div className="chat-composer-tools">
              <select value={provider} onChange={e => { void handleProviderChange(e.target.value) }} className="chat-select chat-select-model">
                {['local', 'deepseek', 'openai', 'gemini', 'bedrock', 'hybrid', 'hybrid2', 'hybrid3'].map(p => <option key={p} value={p}>{p}</option>)}
              </select>
              <div className="flex-1" />

              <button onClick={handleMicToggle}
                title={t(asrListening ? 'Stop voice input' : 'Start voice input')}
                aria-label={t(asrListening ? 'Stop voice input' : 'Start voice input')}
                className="chat-round-button" data-state={asrListening ? 'listening' : undefined}
                disabled={!connected || !sessionReady || sessionSwitching}
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
                className="chat-round-button"
                data-state={visionVideoMode ? 'watching' : pendingVisualAttachment ? 'attached' : undefined}
              >
                <FluentIcon name={visionVideoMode ? 'Video' : 'Photo'} size={16} />
              </button>

              <input
                ref={imageInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={handleVisionFileChange}
              />

              <button onClick={handleSend} disabled={!connected || !sessionReady || sessionSwitching || (!input.trim() && !pendingVisualAttachment)}
                className="chat-send-button"
                aria-label={t('Send')}
              >
                <FluentIcon name="Send" size={16} />
              </button>
            </div>

            {visionWindowPickerOpen && (
              <div className="chat-window-picker">
                <div className="chat-window-picker-header">
                  <span className="chat-window-picker-title">{t('Monitor window')}</span>
                  <SettingsButton
                    size="small"
                    className="ml-auto"
                    onClick={() => loadVisionWindows().catch(() => {})}
                    disabled={visionWindowLoading}
                  >
                    {t('Refresh')}
                  </SettingsButton>
                  <SettingsButton size="small" onClick={handleVisionFullScreenSelect}>
                    {t('Full screen')}
                  </SettingsButton>
                  <button
                    type="button"
                    onClick={() => setVisionWindowPickerOpen(false)}
                    title={t('Close')}
                    aria-label={t('Close')}
                    className="icon-button" data-size="small" data-tone="faint"
                  >
                    <FluentIcon name="Dismiss" size={14} />
                  </button>
                </div>
                <div className="chat-scroll-area chat-window-list">
                  {visionWindowLoading && (
                    <div className="chat-window-status">
                      {t('Loading windows...')}
                    </div>
                  )}
                  {!visionWindowLoading && visionWindowError && (
                    <div className="chat-window-status" data-tone="danger">
                      {visionWindowError}
                    </div>
                  )}
                  {!visionWindowLoading && !visionWindowError && visionWindows.length === 0 && (
                    <div className="chat-window-status">
                      {t('No visible windows found.')}
                    </div>
                  )}
                  {!visionWindowLoading && !visionWindowError && visionWindows.map(win => (
                    <button
                      key={win.hwnd}
                      type="button"
                      onClick={() => handleVisionWindowSelect(win)}
                      className="chat-window-option"
                      data-selected={win.selected || undefined}
                    >
                      <span className="chat-window-option-title">
                        {win.title}
                      </span>
                      {win.processName && (
                        <span className="chat-window-option-meta chat-window-option-process">
                          {win.processName}
                        </span>
                      )}
                      {win.rect && (
                        <span className="chat-window-option-meta">
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
      </div>
      </div>
    </div>
  )

  return (
    <div ref={splitRootRef} className="chat-page flex-1 flex min-h-0">
      {/* Character render area (left, PixiJS mode only) */}
      {renderActive && (
          <div className="crt-render-stage flex flex-col min-w-0" style={{ minWidth: CHARACTER_MIN_W, flex: '1 1 auto' }}>
          {/* PixiJS render iframe */}
          <iframe
            ref={renderFrameRef}
            src={renderAssetUrl || 'about:blank'}
            title={t('Character render')}
            onLoad={handleRenderFrameLoad}
            className="flex-1 border-0"
              style={{ width: '100%', pointerEvents: isSplitResizing ? 'none' : 'auto' }}
          />
          {CRT_WORK_WIDGET_DEMO_ENABLED && <CrtWorkWidget />}

          {/* The legacy Render footer was retired in favor of the single
              Render toggle in the primary sidebar. The protocol endpoints
              remain available to direct integrations and diagnostic routes. */}
        </div>
      )}

      {renderActive && (
        <div
          className={`render-chat-splitter ${isSplitResizing ? 'active' : ''}`}
          role="separator"
          aria-orientation="vertical"
          title={t('Resize render and chat panels')}
          onPointerDown={handleSplitPointerDown}
        />
      )}

      {/* Chat panel (right) */}
      {chatPanel}
    </div>
  )
}
