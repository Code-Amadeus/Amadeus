import { createContext, useContext, useEffect, useState } from 'react'

export interface ActiveCharacter {
  character_id: string
  name: string
  display_name: string
  short_name: string
  ui_name: string
  accessible_name: string
}

type Send = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>

export const ActiveCharacterContext = createContext<ActiveCharacter | null>(null)

export function readActiveCharacter(value: unknown): ActiveCharacter | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const keys = ['character_id', 'name', 'display_name', 'short_name', 'ui_name', 'accessible_name'] as const
  if (keys.some(key => typeof record[key] !== 'string' || !record[key])) return null
  return Object.fromEntries(keys.map(key => [key, record[key]])) as unknown as ActiveCharacter
}

/** One projection per live connection; saved next-launch settings are not identity. */
export function useActiveCharacterSnapshot(connected: boolean, send: Send) {
  const [identity, setIdentity] = useState<ActiveCharacter | null>(null)
  useEffect(() => {
    let current = true
    setIdentity(null)
    if (connected) {
      void send('character.active').then(value => {
        if (current) setIdentity(readActiveCharacter(value))
      }).catch(() => {})
    }
    return () => { current = false }
  }, [connected, send])
  return connected ? identity : null
}

export function useActiveCharacter() {
  return useContext(ActiveCharacterContext)
}
