import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export type UiTheme = 'classic' | 'wallpaper-slice'

const DESKTOP_THEME_KEY = 'AMADEUS_UI_THEME'
const LOCAL_THEME_KEY = 'amadeus.ui.theme'

interface ThemeContextValue {
  theme: UiTheme
  setTheme: (theme: UiTheme) => Promise<void>
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: 'classic',
  setTheme: async () => {},
})

function normalizeTheme(value: unknown): UiTheme {
  return value === 'wallpaper-slice' ? 'wallpaper-slice' : 'classic'
}

function applyTheme(theme: UiTheme): void {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme === 'wallpaper-slice' ? 'dark' : 'light'
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
      const values = snapshot.values && typeof snapshot.values === 'object'
        ? snapshot.values as Record<string, unknown>
        : {}
      const saved = values[DESKTOP_THEME_KEY]
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
    setThemeState(normalized)
    localStorage.setItem(LOCAL_THEME_KEY, normalized)
    applyTheme(normalized)
    if (!window.amadeus) return
    const result = await window.amadeus.updateDesktopSettings({ values: { [DESKTOP_THEME_KEY]: normalized } })
    if (!result.ok) throw new Error(result.error || 'Could not save interface theme')
  }, [])

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext)
}
