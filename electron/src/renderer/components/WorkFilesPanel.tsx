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

  return <section aria-label={t('Files')} className="work-files">
    <h3 className="work-files-heading">{t('Files')} · {files.length}</h3>
    <div className="work-files-list">
      {files.map(file => <article key={file.artifactId} className="work-file-card">
        <div className="flex items-center gap-2">
          <strong className="work-file-title" title={file.relativePath}>{file.relativePath || file.title}</strong>
          <button type="button" className="text-button" disabled={!file.previewable} onClick={() => setSelectedId(file.artifactId)}
            aria-label={t('View {title}', { title: file.title })}>
            {t(file.previewable ? 'View file' : file.artifactStatus === 'missing' ? 'File missing' : 'Preview unavailable')}
          </button>
        </div>
        <p className="work-file-meta truncate">{file.workTitle}</p>
        {file.artifactKind === 'business.export' && <p className="work-file-meta">{t('Approved external export · In-app preview unavailable')}</p>}
        <span className="work-file-facts">
          {t('Execution')}: {file.execution} · {file.workState === 'review_ready' ? t('Awaiting review') : `${t('Work')}: ${file.workState}`} · {file.artifactStatus}
          {file.sizeBytes != null ? ` · ${t('{count} bytes', { count: file.sizeBytes })}` : ''}
        </span>
      </article>)}
    </div>
    {selected?.previewable && <section aria-label={t('File preview: {title}', { title: selected.title })} className="work-file-card work-file-preview">
      <div className="flex items-center gap-2">
        <strong className="work-file-title">{selected.title}</strong>
        <button type="button" className="text-button" data-tone="muted" onClick={() => setSelectedId('')}>{t('Close preview')}</button>
      </div>
      <p className="work-file-facts">{t('Registered revision · Read only · Viewing does not accept this Work.')}</p>
      {currentPreview.error ? <p role="alert" className="work-file-error">{currentPreview.error}</p>
        : currentPreview.content === undefined ? <p role="status" className="work-file-error">{t('Loading file…')}</p>
        : <pre tabIndex={0} className="work-file-content">{currentPreview.content}</pre>}
    </section>}
  </section>
}
