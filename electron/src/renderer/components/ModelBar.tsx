interface Props {
  provider: string
  onProviderChange: (v: string) => void
  ttsMode: string
  onTtsModeChange: (v: string) => void
  showHistory: boolean
  onToggleHistory: () => void
}

import { useI18n } from '../i18n'

const PROVIDERS = ['deepseek', 'openai', 'gemini', 'bedrock', 'hybrid', 'hybrid2', 'hybrid3']
const TTS_MODES = ['gpt_sovits', 'edge']

export default function ModelBar({ provider, onProviderChange, ttsMode, onTtsModeChange, showHistory, onToggleHistory }: Props) {
  const { t } = useI18n()
  return (
    <div className="flex items-center gap-3 px-3 py-1.5 bg-[var(--surface-alt)] border-b border-[var(--border)] h-[36px] shrink-0">
      {/* collapse toggle */}
      <button
        onClick={onToggleHistory}
        className="text-[#666666] hover:text-[#333333] text-xs px-1 transition-colors"
        title={t(showHistory ? 'Hide history' : 'Show history')}
      >
        {showHistory ? '◀' : '▶'}
      </button>

      {/* provider */}
      <div className="flex items-center gap-1.5">
        <label className="text-[11px] text-[#888888]">{t('Model')}</label>
        <select
          value={provider}
          onChange={e => onProviderChange(e.target.value)}
          className="text-[12px] bg-[var(--surface)] border border-[var(--border-strong)] rounded px-1.5 py-0.5 text-[var(--text)]
                     focus:outline-none focus:border-[#0078D4]"
        >
          {PROVIDERS.map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>

      {/* divider */}
      <div className="w-px h-4 bg-[var(--border-strong)]" />

      {/* TTS mode */}
      <div className="flex items-center gap-1.5">
        <label className="text-[11px] text-[#888888]">TTS</label>
        <select
          value={ttsMode}
          onChange={e => onTtsModeChange(e.target.value)}
          className="text-[12px] bg-[var(--surface)] border border-[var(--border-strong)] rounded px-1.5 py-0.5 text-[var(--text)]
                     focus:outline-none focus:border-[#0078D4]"
        >
          {TTS_MODES.map(m => (
            <option key={m} value={m}>{m === 'gpt_sovits' ? 'GPT-SoVITS v3 · Amadeus' : 'Edge TTS'}</option>
          ))}
        </select>
      </div>
    </div>
  )
}
