import { useEffect, useState } from 'react'

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
        ? { error: String(result.error || 'File could not be opened.') }
        : { content: String(result.content ?? '') }) })
    }).catch(error => {
      if (active) setPreview({ artifactId: selected.artifactId, error: error instanceof Error ? error.message : String(error) })
    })
    return () => { active = false }
  }, [selected, read])

  return <section aria-label="Files" style={{ marginBottom: 18 }}>
    <h3 style={{ fontSize: 13, color: 'var(--text)' }}>Files · {files.length}</h3>
    <div style={{ display: 'grid', gap: 8 }}>
      {files.map(file => <article key={file.artifactId} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: 'var(--surface)' }}>
        <div className="flex items-center gap-2">
          <strong className="min-w-0 flex-1 truncate" title={file.relativePath} style={{ fontSize: 12, color: 'var(--text)' }}>{file.relativePath || file.title}</strong>
          <button type="button" disabled={!file.previewable} onClick={() => setSelectedId(file.artifactId)}
            aria-label={`View ${file.title}`} style={{ fontSize: 11, color: 'var(--accent)', cursor: file.previewable ? 'pointer' : 'default' }}>
            {file.previewable ? 'View file' : file.artifactStatus === 'missing' ? 'File missing' : 'Preview unavailable'}
          </button>
        </div>
        <p className="truncate" style={{ margin: '5px 0', fontSize: 10, color: 'var(--muted)' }}>{file.workTitle}</p>
        {file.artifactKind === 'business.export' && <p style={{ margin: '5px 0', fontSize: 10, color: 'var(--muted)' }}>Approved external export · In-app preview unavailable</p>}
        <span style={{ fontSize: 10, color: 'var(--faint)' }}>
          Execution: {file.execution} · {file.workState === 'review_ready' ? 'Awaiting review' : `Work: ${file.workState}`} · {file.artifactStatus}
          {file.sizeBytes != null ? ` · ${file.sizeBytes} bytes` : ''}
        </span>
      </article>)}
    </div>
    {selected?.previewable && <section aria-label={`File preview: ${selected.title}`} style={{ marginTop: 14, border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: 'var(--surface)' }}>
      <div className="flex items-center gap-2">
        <strong className="min-w-0 flex-1 truncate" style={{ color: 'var(--text)', fontSize: 12 }}>{selected.title}</strong>
        <button type="button" onClick={() => setSelectedId('')} style={{ color: 'var(--muted)', fontSize: 11 }}>Close preview</button>
      </div>
      <p style={{ color: 'var(--faint)', fontSize: 10 }}>Registered revision · Read only · Viewing does not accept this Work.</p>
      {currentPreview.error ? <p role="alert" style={{ color: 'var(--muted)', fontSize: 12 }}>{currentPreview.error}</p>
        : currentPreview.content === undefined ? <p role="status">Loading file…</p>
        : <pre tabIndex={0} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflowY: 'auto', color: 'var(--text)', fontSize: 12 }}>{currentPreview.content}</pre>}
    </section>}
  </section>
}
