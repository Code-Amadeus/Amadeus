import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { desktopCatalogFields } from '../shared/configCatalog.js'
import { startupValue } from '../shared/startupSettings.js'

export type UiTheme = 'classic' | 'wallpaper-slice'

const DESKTOP_THEME_KEY = 'AMADEUS_UI_THEME'
const LOCAL_THEME_KEY = 'amadeus.ui.theme'

interface ThemeContextValue {
  theme: UiTheme
  setTheme: (theme: UiTheme) => Promise<void>
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: desktopCatalogFields.AMADEUS_UI_THEME.default as UiTheme,
  setTheme: async () => {},
})

function normalizeTheme(value: unknown): UiTheme {
  return desktopCatalogFields.AMADEUS_UI_THEME.options!.some(option => (typeof option === 'string' ? option : option.value) === value)
    ? value as UiTheme : desktopCatalogFields.AMADEUS_UI_THEME.default as UiTheme
}

function applyTheme(theme: UiTheme): void {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme === 'wallpaper-slice' ? 'dark' : 'light'
  void window.amadeus?.setTitleBarTheme(theme).catch(() => {})
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<UiTheme>(() => {
    const initial = normalizeTheme(localStorage.getItem(LOCAL_THEME_KEY))
    applyTheme(initial)
    return initial
  })

  useEffect(() => {
    let active = true
    void window.amadeus?.getDesktopSettings().then(snapshot => {
      if (!active || !snapshot) return
      const saved = startupValue(DESKTOP_THEME_KEY, snapshot, desktopCatalogFields.AMADEUS_UI_THEME.default)
      if (saved !== undefined) setThemeState(normalizeTheme(saved))
    }).catch(() => {})
    return () => { active = false }
  }, [])

  useEffect(() => {
    localStorage.setItem(LOCAL_THEME_KEY, theme)
    applyTheme(theme)
  }, [theme])

  const setTheme = useCallback(async (nextTheme: UiTheme) => {
    const normalized = normalizeTheme(nextTheme)
    if (!window.amadeus) { setThemeState(normalized); return }
    const result = await window.amadeus.updateDesktopSettings({ values: { [DESKTOP_THEME_KEY]: normalized } })
    if (!result.ok) throw new Error(result.error || 'Could not save interface theme')
    setThemeState(normalized)
  }, [])

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext)
}
