import React from 'react'
import ReactDOM from 'react-dom/client'
import VNPage from '../../src/renderer/components/VNPage'
import { I18nProvider } from '../../src/renderer/i18n'
import '../../src/renderer/styles/index.css'

// Let Vite resolve both production imports together, including HMR versions.
// Inline HTML imports can accidentally create two distinct I18n contexts.
const host = window as any
host.vnSubscribers = {}
const send = (method: string, params: Record<string, unknown> = {}) => host.backend(method, params)
const subscribe = (method: string, fn: (payload: Record<string, unknown>) => void) => {
  (host.vnSubscribers[method] ||= []).push(fn)
  return () => { host.vnSubscribers[method] = host.vnSubscribers[method].filter((item: unknown) => item !== fn) }
}
host.amadeus = {
  selectVNFile: (kind: string) => host.pickFile(kind),
  openVNHelp: async () => {},
  getDesktopSettings: async () => ({ values: {} }),
}
ReactDOM.createRoot(document.getElementById('root')!).render(<I18nProvider><VNPage send={send} subscribe={subscribe} connected /></I18nProvider>)
