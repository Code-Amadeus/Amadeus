import zhCN from './locales/zh-CN.json'
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { catalogTranslations, desktopCatalogFields } from '../shared/configCatalog.js'

import { startupValue } from '../shared/startupSettings.js'

export type UiLocale = 'en-US' | 'zh-CN'

const DESKTOP_LOCALE_KEY = 'AMADEUS_UI_LOCALE'
const LOCAL_LOCALE_KEY = 'amadeus.ui.locale'

const ZH_CN: Record<string, string> = { ...catalogTranslations, ...zhCN }

type TranslateVariables = Record<string, string | number>

interface I18nContextValue {
  locale: UiLocale
  setLocale: (locale: UiLocale) => Promise<void>
  t: (source: string, variables?: TranslateVariables) => string
}

const I18nContext = createContext<I18nContextValue>({
  locale: desktopCatalogFields.AMADEUS_UI_LOCALE.default as UiLocale,
  setLocale: async () => {},
  t: source => source,
})

function normalizeLocale(value: unknown): UiLocale {
  return desktopCatalogFields.AMADEUS_UI_LOCALE.options!.some(option => (typeof option === 'string' ? option : option.value) === value)
    ? value as UiLocale : desktopCatalogFields.AMADEUS_UI_LOCALE.default as UiLocale
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<UiLocale>(() => normalizeLocale(localStorage.getItem(LOCAL_LOCALE_KEY)))

  useEffect(() => {
    let active = true
    void window.amadeus?.getDesktopSettings().then(snapshot => {
      if (!active || !snapshot) return
      const saved = startupValue(DESKTOP_LOCALE_KEY, snapshot, desktopCatalogFields.AMADEUS_UI_LOCALE.default)
      if (saved !== undefined) setLocaleState(normalizeLocale(saved))
    }).catch(() => {})
    return () => { active = false }
  }, [])

  useEffect(() => {
    localStorage.setItem(LOCAL_LOCALE_KEY, locale)
    document.documentElement.lang = locale
  }, [locale])

  const setLocale = useCallback(async (nextLocale: UiLocale) => {
    const normalized = normalizeLocale(nextLocale)
    if (!window.amadeus) { setLocaleState(normalized); return }
    const result = await window.amadeus.updateDesktopSettings({ values: { [DESKTOP_LOCALE_KEY]: normalized } })
    if (!result.ok) throw new Error(result.error || 'Could not save console language')
    setLocaleState(normalized)
  }, [])

  const t = useCallback((source: string, variables: TranslateVariables = {}) => {
    let result = locale === 'zh-CN' ? ZH_CN[source] || source : source
    for (const [key, value] of Object.entries(variables)) {
      result = result.replaceAll(`{${key}}`, () => String(value))
    }
    return result
  }, [locale])

  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t])
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  return useContext(I18nContext)
}
