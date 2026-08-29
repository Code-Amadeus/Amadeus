import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles/index.css'

const workPreviewWindow = new URLSearchParams(window.location.search).get('previewWindow') === '1'
document.documentElement.classList.toggle('work-preview-window', workPreviewWindow)
document.body.classList.toggle('work-preview-window', workPreviewWindow)

ReactDOM.createRoot(document.getElementById('root')!).render(<App />)
