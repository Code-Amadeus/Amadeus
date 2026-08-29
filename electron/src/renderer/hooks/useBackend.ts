/**
 * WebSocket hook for backend communication.
 *
 * Usage:
 *   const { send, subscribe, connected } = useBackend()
 *
 *   // Send a request and get the response
 *   send('chat.send', { text: 'hello' }).then(resp => ...)
 *
 *   // Subscribe to server-pushed events
 *   subscribe('chat.token', (params) => { ... })
 */

import { useEffect, useRef, useCallback, useState } from 'react'

type Envelope = {
  type: 'req' | 'evt' | 'res'
  id: string
  method: string
  params: Record<string, unknown>
}

function warnForLongBackendMessage(
  msg: Envelope,
  data: unknown,
  startedAt: number,
  parsedAt: number,
) {
  const finishedAt = performance.now()
  if (finishedAt - startedAt < 50) return
  console.warn('[backend-message-long-task]', {
    method: msg.method,
    payloadChars: typeof data === 'string' ? data.length : 0,
    parseMs: Math.round((parsedAt - startedAt) * 10) / 10,
    dispatchMs: Math.round((finishedAt - parsedAt) * 10) / 10,
  })
}

type Subscriber = (params: Record<string, unknown>) => void

type BackendConnection = {
  url: string
  protocols: string[]
}

const RECONNECT_DELAY = 2000
const MAX_RECONNECT_DELAY = 30000

export function useBackend() {
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const subscribersRef = useRef<Map<string, Set<Subscriber>>>(new Map())
  const pendingRef = useRef<Map<string, {
    resolve: (v: Record<string, unknown>) => void
    reject: (e: Error) => void
    timer: ReturnType<typeof setTimeout>
  }>>(new Map())
  const reconnectDelay = useRef(RECONNECT_DELAY)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const connectionRef = useRef<BackendConnection | null>(null)

  const connect = useCallback((connection: BackendConnection) => {
    connectionRef.current = connection
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(
      connection.url,
      connection.protocols.length ? connection.protocols : undefined,
    )
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      reconnectDelay.current = RECONNECT_DELAY
    }

    ws.onmessage = (event) => {
      const startedAt = performance.now()
      const msg: Envelope = JSON.parse(event.data)
      const parsedAt = performance.now()

      if (msg.type === 'res') {
        const pending = pendingRef.current.get(msg.id)
        if (pending) {
          clearTimeout(pending.timer)
          pendingRef.current.delete(msg.id)
          if (msg.params?.error) {
            pending.reject(new Error(String(msg.params.error)))
          } else {
            pending.resolve(msg.params || {})
          }
        }
        warnForLongBackendMessage(msg, event.data, startedAt, parsedAt)
        return
      }

      // server-pushed events
      if (msg.type === 'evt') {
        const subs = subscribersRef.current.get(msg.method)
        if (subs) {
          for (const fn of subs) fn(msg.params || {})
        }
      }
      warnForLongBackendMessage(msg, event.data, startedAt, parsedAt)
    }

    ws.onclose = () => {
      setConnected(false)
      // reject all pending
      for (const [, p] of pendingRef.current) {
        clearTimeout(p.timer)
        p.reject(new Error('disconnected'))
      }
      pendingRef.current.clear()
      wsRef.current = null

      // auto-reconnect
      if (connectionRef.current) {
        reconnectTimer.current = setTimeout(() => {
          connect(connectionRef.current!)
          reconnectDelay.current = Math.min(
            reconnectDelay.current * 1.5, MAX_RECONNECT_DELAY
          )
        }, reconnectDelay.current)
      }
    }

    ws.onerror = () => { /* onclose handles cleanup */ }
  }, [])

  const send = useCallback((method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> => {
    return new Promise((resolve, reject) => {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        reject(new Error('not connected'))
        return
      }
      const id = crypto.randomUUID()
      const timer = setTimeout(() => {
        pendingRef.current.delete(id)
        reject(new Error('timeout'))
      }, 30000)
      pendingRef.current.set(id, { resolve, reject, timer })
      ws.send(JSON.stringify({ type: 'req', id, method, params }))
    })
  }, [])

  const subscribe = useCallback((method: string, fn: Subscriber): () => void => {
    const map = subscribersRef.current
    if (!map.has(method)) map.set(method, new Set())
    map.get(method)!.add(fn)
    return () => { map.get(method)?.delete(fn) }
  }, [])

  // cleanup on unmount
  useEffect(() => {
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      wsRef.current?.close()
    }
  }, [])

  // auto-connect to Python backend (via Electron IPC or direct URL)
  useEffect(() => {
    const init = async () => {
      const amadeus = window.amadeus
      try {
        if (amadeus) {
          const connection = await amadeus.getBackendConnection()
          if (!connection) throw new Error('backend instance is not authenticated')
          connect({ url: connection.url, protocols: connection.protocols })
          return
        }
        // Browser-only development retains the direct loopback contract. The
        // corresponding Python backend runs in explicit development mode.
        connect({ url: 'ws://127.0.0.1:17777/ws', protocols: [] })
      } catch (error) {
        console.error('[backend-connection] unavailable', error)
      }
    }
    init()
  }, [connect])

  return { send, subscribe, connected }
}
