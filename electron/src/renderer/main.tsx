import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { I18nProvider } from './i18n'
import { ThemeProvider } from './theme'
import './styles/index.css'

const workPreviewWindow = new URLSearchParams(window.location.search).get('previewWindow') === '1'
document.documentElement.classList.toggle('work-preview-window', workPreviewWindow)
document.body.classList.toggle('work-preview-window', workPreviewWindow)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <ThemeProvider>
    <I18nProvider><App /></I18nProvider>
  </ThemeProvider>,
)
