import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { desktopCatalogFields } from '../shared/configCatalog.js'
import type { BuiltinTheme, DesktopThemeState } from '../shared/extensionTheme.js'
import { createThemeApplication } from './themeApplication.js'

export type UiTheme = BuiltinTheme
const DEFAULT_THEME = desktopCatalogFields.AMADEUS_UI_THEME.default as UiTheme
const LOCAL_THEME_KEY = 'amadeus.ui.theme'
interface ThemeContextValue {
  theme: UiTheme
  selection: string
  reason: string
  appliedVersion?: string
  setTheme: (theme: string) => Promise<void>
}
const ThemeContext = createContext<ThemeContextValue>({ theme: DEFAULT_THEME, selection: DEFAULT_THEME, reason: '', setTheme: async () => {} })

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DesktopThemeState>(() => {
    const cached = localStorage.getItem(LOCAL_THEME_KEY)
    const base = cached === 'classic' || cached === 'wallpaper-slice' ? cached : DEFAULT_THEME
    return { selected: base, base, reason: '' }
  })
  const application = useMemo(() => createThemeApplication(document), [])
  const generation = useRef(0)
  const refresh = useCallback(async () => {
    const own = ++generation.current
    if (!window.amadeus) return
    const next = await window.amadeus.getDesktopTheme()
    if (next && own === generation.current) setState(next)
  }, [])
  useEffect(() => {
    void refresh().catch(() => {})
    const remove = window.amadeus?.onDesktopThemeChanged(() => { void refresh().catch(() => {}) })
    return () => { generation.current++; remove?.() }
  }, [refresh])
  useLayoutEffect(() => {
    try { application.apply(state) }
    catch {
      application.dispose()
      setState({ selected: state.selected, base: state.base, reason: 'The theme could not be displayed. Its base is shown.' })
    }
    // Cache only a verified built-in base; extension tokens are never trusted from localStorage.
    localStorage.setItem(LOCAL_THEME_KEY, state.base)
    void window.amadeus?.setTitleBarTheme(state.base).catch(() => {})
  }, [state, application])
  useEffect(() => () => application.dispose(), [application])
  const setTheme = useCallback(async (selection: string) => {
    if (!window.amadeus) {
      if (selection === 'classic' || selection === 'wallpaper-slice') setState({ selected: selection, base: selection, reason: '' })
      return
    }
    const result = await window.amadeus.updateDesktopSettings({ values: { AMADEUS_UI_THEME: selection } })
    if (!result.ok) throw new Error(result.error || 'Could not save interface theme')
    await refresh()
  }, [refresh])
  const value = useMemo(() => ({ theme: state.base, selection: state.selected, reason: state.reason, appliedVersion: state.theme?.version, setTheme }), [state, setTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
export function useTheme(): ThemeContextValue { return useContext(ThemeContext) }
