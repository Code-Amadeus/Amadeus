import { useEffect, useState } from 'react'
import { useI18n } from '../i18n'

export interface WorkFileSummary {
  projectId: string
  workItemId: string
  workTitle: string
  workState: string
  execution: string
  artifactId: string
  title: string
  relativePath: string
  artifactStatus: string
  artifactKind?: string
  sizeBytes?: number
  previewable: boolean
}

interface Props {
  files: WorkFileSummary[]
  read: (file: WorkFileSummary) => Promise<Record<string, unknown>>
}

export default function WorkFilesPanel({ files, read }: Props) {
  const { t } = useI18n()
  const [selectedId, setSelectedId] = useState('')
  const [preview, setPreview] = useState<{ artifactId?: string; content?: string; error?: string }>({})
  const selected = files.find(file => file.artifactId === selectedId)
  const currentPreview = preview.artifactId === selectedId ? preview : {}
  useEffect(() => {
    let active = true
    setPreview({})
    if (selected?.previewable) void read(selected).then(result => {
      if (!active) return
      setPreview({ artifactId: selected.artifactId, ...(result.ok === false
        ? { error: String(result.error || t('File could not be opened.')) }
        : { content: String(result.content ?? '') }) })
    }).catch(error => {
      if (active) setPreview({ artifactId: selected.artifactId, error: error instanceof Error ? error.message : String(error) })
    })
    return () => { active = false }
    // Catalog projections may create fresh objects on unrelated parent renders.
    // Only changing the selected artifact/owner or its availability reloads it.
  }, [selected?.artifactId, selected?.workItemId, selected?.previewable, read])

  return <section aria-label={t('Files')} style={{ marginBottom: 18 }}>
    <h3 style={{ fontSize: 13, color: 'var(--text)' }}>{t('Files')} · {files.length}</h3>
    <div style={{ display: 'grid', gap: 8 }}>
      {files.map(file => <article key={file.artifactId} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: 'var(--surface)' }}>
        <div className="flex items-center gap-2">
          <strong className="min-w-0 flex-1 truncate" title={file.relativePath} style={{ fontSize: 12, color: 'var(--text)' }}>{file.relativePath || file.title}</strong>
          <button type="button" disabled={!file.previewable} onClick={() => setSelectedId(file.artifactId)}
            aria-label={t('View {title}', { title: file.title })} style={{ fontSize: 11, color: 'var(--accent)', cursor: file.previewable ? 'pointer' : 'default' }}>
            {t(file.previewable ? 'View file' : file.artifactStatus === 'missing' ? 'File missing' : 'Preview unavailable')}
          </button>
        </div>
        <p className="truncate" style={{ margin: '5px 0', fontSize: 10, color: 'var(--muted)' }}>{file.workTitle}</p>
        {file.artifactKind === 'business.export' && <p style={{ margin: '5px 0', fontSize: 10, color: 'var(--muted)' }}>{t('Approved external export · In-app preview unavailable')}</p>}
        <span style={{ fontSize: 10, color: 'var(--faint)' }}>
          {t('Execution')}: {file.execution} · {file.workState === 'review_ready' ? t('Awaiting review') : `${t('Work')}: ${file.workState}`} · {file.artifactStatus}
          {file.sizeBytes != null ? ` · ${t('{count} bytes', { count: file.sizeBytes })}` : ''}
        </span>
      </article>)}
    </div>
    {selected?.previewable && <section aria-label={t('File preview: {title}', { title: selected.title })} style={{ marginTop: 14, border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: 'var(--surface)' }}>
      <div className="flex items-center gap-2">
        <strong className="min-w-0 flex-1 truncate" style={{ color: 'var(--text)', fontSize: 12 }}>{selected.title}</strong>
        <button type="button" onClick={() => setSelectedId('')} style={{ color: 'var(--muted)', fontSize: 11 }}>{t('Close preview')}</button>
      </div>
      <p style={{ color: 'var(--faint)', fontSize: 10 }}>{t('Registered revision · Read only · Viewing does not accept this Work.')}</p>
      {currentPreview.error ? <p role="alert" style={{ color: 'var(--muted)', fontSize: 12 }}>{currentPreview.error}</p>
        : currentPreview.content === undefined ? <p role="status">{t('Loading file…')}</p>
        : <pre tabIndex={0} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflowY: 'auto', color: 'var(--text)', fontSize: 12 }}>{currentPreview.content}</pre>}
    </section>}
  </section>
}
