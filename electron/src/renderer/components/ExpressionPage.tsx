import { useState, useEffect, useCallback } from 'react'

interface PresetField {
  name: string
  value: number
  min: number
  max: number
  step: number
  description?: string
}

interface Preset {
  name: string
  fields: PresetField[]
  auto_return_idle?: boolean
  description?: string
}

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
}

export default function ExpressionPage({ send, subscribe }: Props) {
  const [presets, setPresets] = useState<Preset[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, number>>({})
  const [autoReturn, setAutoReturn] = useState(false)

  useEffect(() => {
    const unsub = subscribe('expression.presets', (p) => {
      const list = (p.presets ?? p) as any
      if (Array.isArray(list)) {
        const parsed: Preset[] = list.map((item: any) => ({
          name: item.name ?? 'Unnamed',
          fields: Object.entries(item.params ?? item).map(([k, v]: [string, any]) => ({
            name: k,
            value: typeof v === 'number' ? v : (v?.default ?? v?.value ?? 0),
            min: v?.min ?? 0,
            max: v?.max ?? 1,
            step: v?.step ?? 0.05,
            description: v?.description ?? '',
          })),
          auto_return_idle: item.auto_return_idle ?? false,
          description: item.description ?? '',
        }))
        setPresets(parsed)
        if (parsed.length > 0 && !selected) {
          setSelected(parsed[0].name)
          initValues(parsed[0])
        }
      }
    })
    send('expression.presets', {}).catch(() => {})
    return unsub
  }, [subscribe, send, selected])

  const initValues = useCallback((preset: Preset) => {
    const v: Record<string, number> = {}
    preset.fields.forEach(f => { v[f.name] = f.value })
    setValues(v)
    setAutoReturn(preset.auto_return_idle ?? false)
  }, [])

  const selectedPreset = presets.find(p => p.name === selected)

  const handleSelect = useCallback((name: string) => {
    setSelected(name)
    const p = presets.find(p => p.name === name)
    if (p) initValues(p)
  }, [presets, initValues])

  const handleSlider = useCallback((field: string, value: number) => {
    setValues(prev => ({ ...prev, [field]: value }))
  }, [])

  const handleTest = useCallback(async () => {
    if (!selected) return
    await send('expression.trigger', { name: selected, params: values })
  }, [send, selected, values])

  return (
    <div className="flex-1 flex min-h-0">
      {/* Left: compact preset list */}
      <div
        className="flex flex-col shrink-0"
        style={{ width: 190, backgroundColor: '#FAFAFA', borderRight: '1px solid #E0E0E0' }}
      >
        <div
          className="text-[13px] font-bold shrink-0"
          style={{ color: '#555', padding: '16px 14px 8px 14px' }}
        >
          Presets
        </div>
        <div className="flex-1 overflow-y-auto">
          {presets.map(p => (
            <button
              key={p.name}
              onClick={() => handleSelect(p.name)}
              className="w-full text-left text-[13px] border-b border-[#F0F0F0] transition-colors"
              style={{
                padding: '10px 16px',
                color: selected === p.name ? '#0078D4' : '#555',
                backgroundColor: selected === p.name ? '#E3F2FD' : 'transparent',
                borderRadius: 4,
              }}
              onMouseEnter={e => {
                if (selected !== p.name)
                  (e.currentTarget as HTMLElement).style.backgroundColor = '#F5F5F5'
              }}
              onMouseLeave={e => {
                if (selected !== p.name)
                  (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent'
              }}
            >
              {p.name}
            </button>
          ))}
          {presets.length === 0 && (
            <p className="px-4 py-4 text-[12px] italic" style={{ color: '#AAA' }}>
              No presets loaded
            </p>
          )}
        </div>
      </div>

      {/* Right: expression editor */}
      <div className="flex-1 overflow-y-auto" style={{ padding: '28px 36px' }}>
        {!selectedPreset ? (
          <p className="text-center mt-20" style={{ color: '#AAA', fontSize: 13 }}>
            Select a preset from the left panel.
          </p>
        ) : (
          <div className="max-w-lg">
            {/* title row */}
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-semibold" style={{ fontSize: 18, color: 'var(--text)' }}>
                {selectedPreset.name}
              </h3>
              <div className="flex gap-2">
                <button
                  onClick={handleTest}
                  className="px-4 py-1.5 text-[13px] font-[500] text-white rounded"
                  style={{ backgroundColor: '#0078D4', height: 32, width: 80 }}
                >
                  Test
                </button>
              </div>
            </div>

            {/* separator */}
            <div style={{ height: 1, backgroundColor: '#E8E8E8', margin: '12px 0' }} />

            {/* fields — spacing 20px */}
            <div className="flex flex-col" style={{ gap: 20 }}>
              {selectedPreset.fields.map(f => (
                <div key={f.name}>
                  {/* label + value */}
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-[13px] font-[500]" style={{ color: '#444' }}>
                      {f.name}
                    </label>
                    <span
                      className="text-[13px] font-bold tabular-nums text-right"
                      style={{ color: '#0078D4', width: 70 }}
                    >
                      {values[f.name]?.toFixed(2) ?? f.value.toFixed(2)}
                    </span>
                  </div>

                  {/* slider */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] tabular-nums text-right" style={{ color: '#AAA', width: 32 }}>
                      {f.min}
                    </span>
                    <input
                      type="range"
                      min={f.min}
                      max={f.max}
                      step={f.step}
                      value={values[f.name] ?? f.value}
                      onChange={e => handleSlider(f.name, parseFloat(e.target.value))}
                      className="flex-1"
                      disabled={!autoReturn && f.name === 'idle_return_delay_sec'}
                    />
                    <span className="text-[11px] tabular-nums" style={{ color: '#AAA', width: 32 }}>
                      {f.max}
                    </span>
                  </div>

                  {f.description && (
                    <p className="text-[11px] mt-0.5" style={{ color: '#999' }}>{f.description}</p>
                  )}
                </div>
              ))}
            </div>

            {/* auto return checkbox */}
            <div style={{ height: 1, backgroundColor: '#E8E8E8', margin: '24px 0 0 0' }} />
            <div className="flex items-center gap-2 pt-4">
              <input
                type="checkbox"
                id="autoReturn"
                checked={autoReturn}
                onChange={e => setAutoReturn(e.target.checked)}
                className="accent-[#0078D4]"
              />
              <label htmlFor="autoReturn" className="text-[13px]" style={{ color: '#444', fontWeight: 500 }}>
                Auto Return to Idle
              </label>
            </div>
            <p className="text-[11px] mt-1 ml-6" style={{ color: '#999' }}>
              Fade out after delay — otherwise stays until next turn
            </p>

            {selectedPreset.description && (
              <p className="text-[11px] mt-4" style={{ color: '#BBB' }}>
                {selectedPreset.description}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
