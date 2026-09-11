import { useCallback, useEffect, useRef, useState } from 'react'
import { freshLayoutProfile, LAYOUT_STORAGE_KEY, readLayoutPreferences, saveLayoutProfile,
  type CompanionDesktop, type EditMode, type LayoutMode, type LayoutProfile } from './companionLayoutPreferences'

/** Presentation preferences only. Edit locks are intentionally never saved. */
export function useCompanionLayout() {
  const [preferences, setPreferences] = useState(() => readLayoutPreferences(localStorage.getItem(LAYOUT_STORAGE_KEY)))
  const [desktop, setDesktop] = useState<CompanionDesktop | null>(null)
  const display = desktop?.key || ''
  const [edit, setEdit] = useState<EditMode>('locked')
  const [panelOpen, setPanelOpen] = useState(false)
  const fresh = useRef(freshLayoutProfile())
  const mode = preferences.mode
  const profile = preferences.displays[display]?.[mode] || preferences.displays[desktop?.legacyKey || '']?.[mode] || fresh.current
  const latest = useRef(preferences)
  latest.current = preferences

  useEffect(() => {
    let live = true
    const measure = () => void window.amadeus?.getFloatingCompanionDisplay().then(info => {
      if (live && info) { setDesktop(info); setEdit('locked') }
    })
    measure()
    const off = window.amadeus?.onFloatingCompanionDisplayChanged(measure)
    return () => { live = false; off?.() }
  }, [])
  useEffect(() => {
    if (!display) return
    // Avoid synchronous disk-backed storage on every pointer movement.
    const save = () => localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(latest.current))
    const timer = window.setTimeout(save, 250)
    window.addEventListener('pagehide', save)
    return () => { window.clearTimeout(timer); window.removeEventListener('pagehide', save) }
  }, [preferences, display])
  useEffect(() => {
    if (display && edit === 'locked') localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(latest.current))
  }, [edit, display])
  const updateProfile = useCallback((change: (before: LayoutProfile) => LayoutProfile) => {
    if (!display) return
    setPreferences(before => {
      const old = before.displays[display]?.[before.mode] || before.displays[desktop?.legacyKey || '']?.[before.mode] || fresh.current
      const next = change(old)
      return next === old ? before : saveLayoutProfile(before, display, before.mode, next)
    })
  }, [display, desktop?.legacyKey])
  const selectMode = (next: LayoutMode) => { setEdit('locked'); setPreferences(before => ({ ...before, mode: next })) }
  const resetLayout = () => updateProfile(before => ({ ...before, slots: [], offsets: { projects: {}, tasks: {} }, placements: undefined }))
  const shuffle = () => updateProfile(before => ({ ...before, seed: before.seed + 1, slots: [], offsets: { projects: {}, tasks: {} }, placements: undefined }))
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setEdit('locked'); setPanelOpen(false) } }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [])
  return { mode, profile, display, desktop, edit, setEdit, panelOpen, setPanelOpen, selectMode, resetLayout, shuffle, updateProfile }
}
export type CompanionLayoutController = ReturnType<typeof useCompanionLayout>
