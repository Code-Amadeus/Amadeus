import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import FluentIcon from './FluentIcon'
import { CardShell, SettingsButton, SettingsField, StatusPill } from './SettingsPrimitives'
import { useI18n } from '../i18n'
import {
  MemoryPanelConnection, MemoryPanelFailure, memoryAttempt, memoryErrorText, memoryIndexText,
  memoryAutomaticAdmissionText, memoryLifecycleText, memorySemanticReasonText, selectedSourceSpan, sourceSpan, sourceTextParts,
  memorySourcePolicyReasonText, memorySourcePolicyActionText, memoryPolicyRecoveryCommand, suggestionKindText,
  type MemoryAttempt, type MemoryScope, type MemorySend, type MemorySource,
  type MemoryStatus, type SavedMemory, type MemoryForgetPreview, type MemoryCursor,
  type MemorySuggestion, type MemorySuggestionDetail,
} from './memoryPanelState'
import '../styles/memoryPanel.css'

interface Props {
  sessionId: string
  characterName: string
  projectName?: string
  visible: boolean
  send: MemorySend
  onClose: () => void
}
interface Detail { memory: SavedMemory; history: SavedMemory[] }
interface ScopeOption { kind: MemoryScope; available: boolean }
type Editor = 'inspect' | 'new' | 'correct'

export default function MemoryPanel({ sessionId, characterName, projectName, visible, send, onClose }: Props) {
  const { t } = useI18n()
  const connection = useMemo(() => new MemoryPanelConnection(sessionId, send), [sessionId, send])
  const [enabled, setEnabled] = useState(false)
  const [status, setStatus] = useState<MemoryStatus>({})
  const [scopes, setScopes] = useState<ScopeOption[]>([])
  const [scope, setScope] = useState<MemoryScope>('character')
  const [items, setItems] = useState<SavedMemory[]>([])
  const [nextCursor, setNextCursor] = useState<MemoryCursor | null>(null)
  const [pageLoading, setPageLoading] = useState(false)
  const [sources, setSources] = useState<MemorySource[]>([])
  const [sourcesAvailable, setSourcesAvailable] = useState(false)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [reviewAvailable, setReviewAvailable] = useState(false)
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [suggestions, setSuggestions] = useState<MemorySuggestion[]>([])
  const [reviewTarget, setReviewTarget] = useState<MemorySuggestion | null>(null)
  const [suggestion, setSuggestion] = useState<MemorySuggestionDetail | null>(null)
  const [editor, setEditor] = useState<Editor>('inspect')
  const [text, setText] = useState('')
  const [correctionMode, setCorrectionMode] = useState<'change' | 'error'>('change')
  const [sourceIndex, setSourceIndex] = useState(-1)
  const [range, setRange] = useState({ start: 0, end: 0 })
  const [selection, setSelection] = useState<{ start: number; end: number } | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [receipt, setReceipt] = useState('')
  const [pending, setPending] = useState<MemoryAttempt | null>(null)
  const [forgetPreview, setForgetPreview] = useState<MemoryForgetPreview | null>(null)
  const reads = useRef(0)
  const listGeneration = useRef(0)
  const pageRequest = useRef({ serial: 0, active: false })
  const mutationInFlight = useRef(false)
  const source = sources[sourceIndex]
  const span = source ? sourceSpan(source, range.start, range.end) : null
  const editing = editor !== 'inspect'
  const locked = loading || busy || Boolean(pending)

  // This binding belongs only to the keyed Session/character/project panel, including hidden panels.
  useEffect(() => () => connection.dispose(), [connection])

  const load = useCallback(async (nextScope: MemoryScope) => {
    const revision = ++reads.current
    ++listGeneration.current
    pageRequest.current.serial++; pageRequest.current.active = false
    setLoading(true); setPageLoading(false); setItems([]); setNextCursor(null); setError('')
    setSuggestions([]); setReviewTarget(null); setSuggestion(null)
    try {
      const snapshot = await connection.request('memory.status')
      if (!connection.active || revision !== reads.current) return
      setEnabled(snapshot.enabled === true)
      setStatus((snapshot.status as MemoryStatus) || {})
      setScopes(Array.isArray(snapshot.scopes) ? snapshot.scopes as ScopeOption[] : [])
      setReviewAvailable(snapshot.candidate_review_available === true)
      if (snapshot.enabled !== true) { setItems([]); setSources([]); return }
      if (showSuggestions) {
        if (snapshot.candidate_review_available !== true) { setShowSuggestions(false); return }
        const result = await connection.request('memory.candidates')
        if (connection.active && revision === reads.current) {
          setSuggestions(Array.isArray(result.candidates) ? result.candidates as MemorySuggestion[] : [])
        }
        return
      }
      const [list, sourceList] = await Promise.allSettled([
        connection.request('memory.list', { scope: nextScope, limit: 100 }),
        connection.request('memory.sources', { limit: 50 }),
      ])
      if (!connection.active || revision !== reads.current) return
      if (list.status === 'rejected') throw list.reason
      setItems(Array.isArray(list.value.items) ? list.value.items as SavedMemory[] : [])
      setNextCursor(Array.isArray(list.value.next_cursor) ? list.value.next_cursor as MemoryCursor : null)
      setSourcesAvailable(sourceList.status === 'fulfilled')
      setSources(sourceList.status === 'fulfilled' && Array.isArray(sourceList.value.sources)
        ? sourceList.value.sources as MemorySource[] : [])
    } catch (failure) {
      if (connection.active && revision === reads.current) {
        setError(memoryErrorText(failure)); setEnabled(false); setItems([])
      }
    } finally { if (connection.active && revision === reads.current) setLoading(false) }
  }, [connection, showSuggestions])

  useEffect(() => { void load(scope) }, [load, scope])

  const loadMore = async () => {
    if (locked || !nextCursor || pageRequest.current.active) return
    const generation = listGeneration.current
    const request = ++pageRequest.current.serial
    pageRequest.current.active = true
    setPageLoading(true); setError('')
    try {
      const result = await connection.request('memory.list', { scope, limit: 100, cursor: nextCursor })
      if (!connection.active || generation !== listGeneration.current) return
      const page = Array.isArray(result.items) ? result.items as SavedMemory[] : []
      setItems(previous => [...new Map([...previous, ...page].map(item => [item.memory_id, item])).values()])
      setNextCursor(Array.isArray(result.next_cursor) ? result.next_cursor as MemoryCursor : null)
    } catch (failure) {
      if (connection.active && generation === listGeneration.current) setError(memoryErrorText(failure))
    } finally {
      if (connection.active && request === pageRequest.current.serial) {
        pageRequest.current.active = false; setPageLoading(false)
      }
    }
  }

  const inspect = async (memoryId: string) => {
    const revision = ++reads.current
    setLoading(true); setError(''); setReceipt(''); setForgetPreview(null)
    try {
      const result = await connection.request('memory.get', { memory_id: memoryId })
      if (!connection.active || revision !== reads.current) return
      if (!result.memory || typeof (result.memory as SavedMemory).text !== 'string') throw new Error('Missing memory detail')
      setDetail({ memory: result.memory as SavedMemory,
        history: Array.isArray(result.history) ? result.history as SavedMemory[] : [] })
      setEditor('inspect')
    } catch (failure) {
      if (connection.active && revision === reads.current) setError(memoryErrorText(failure))
    } finally { if (connection.active && revision === reads.current) setLoading(false) }
  }

  const inspectSuggestion = async (item: MemorySuggestion) => {
    if (locked) return
    const revision = ++reads.current
    setLoading(true); setError(''); setReceipt(''); setReviewTarget(item); setSuggestion(null)
    try {
      const result = await connection.request('memory.candidate', {
        candidate_id: item.candidate_id, expected_version: item.row_version,
      })
      if (!connection.active || revision !== reads.current) return
      const reviewed = result.candidate as MemorySuggestionDetail | undefined
      if (!reviewed || reviewed.candidate_id !== item.candidate_id || reviewed.row_version !== item.row_version
        || typeof reviewed.text !== 'string' || !Array.isArray(reviewed.quotes) || !reviewed.quotes.length) {
        throw new Error('Incomplete suggestion')
      }
      setSuggestion(reviewed)
    } catch (failure) {
      if (connection.active && revision === reads.current) setError(memoryErrorText(failure))
    } finally { if (connection.active && revision === reads.current) setLoading(false) }
  }

  const begin = (next: 'new' | 'correct') => {
    if (locked) return
    setEditor(next); setText(next === 'correct' ? detail?.memory.text || '' : '')
    if (next === 'new') setDetail(null)
    setCorrectionMode('change'); setSourceIndex(-1); setRange({ start: 0, end: 0 })
    setForgetPreview(null); setSelection(null); setError(''); setReceipt('')
  }

  const perform = async (attempt: MemoryAttempt) => {
    if (mutationInFlight.current || !connection.active) return
    mutationInFlight.current = true
    setBusy(true); setPending(attempt); setError(''); setReceipt('')
    try {
      const result = await connection.request(attempt.method, { ...attempt.params })
      if (!connection.active) return
      const operation = result.operation as { status?: string; index_state?: string; memory_id?: string; cleanup_pending?: boolean } | undefined
      const messages: Record<string, string> = {
        saved: 'Memory saved durably.',
        corrected: 'Correction saved durably.',
        forgotten: Array.isArray(attempt.params.confirmed_targets) && attempt.params.confirmed_targets.length > 1
          ? 'The listed memories and their historical versions were forgotten.' : 'This memory and its historical versions were forgotten.',
        accepted: 'Suggestion saved as a memory.',
        rejected: 'Suggestion dismissed.',
        retired: 'This earlier operation was retired. It did not restore a forgotten memory.',
      }
      if (!operation?.status || !Object.hasOwn(messages, operation.status)) {
        throw new Error('Unconfirmed operation')
      }
      setPending(null); setForgetPreview(null); setEditor('inspect'); setText(''); setSourceIndex(-1)
      setDetail(result.memory ? { memory: result.memory as SavedMemory, history: [] } : null)
      if (operation.cleanup_pending) setStatus(value => ({ ...value, cleanup_state: 'pending' }))
      setReceipt(messages[operation.status])
      // Refresh derived readiness and cleanup facts separately from the committed operation receipt.
      await load(scope)
    } catch (failure) {
      if (!connection.active) return
      setError(memoryErrorText(failure))
      // Only known no-commit rejections clear the body. Busy may follow an earlier uncertain retry.
      if (failure instanceof MemoryPanelFailure && failure.definite) setPending(null)
      if (failure instanceof MemoryPanelFailure && ['conflict', 'forget_preview_stale', 'forget_confirmation_required'].includes(failure.code)) setForgetPreview(null)
    } finally {
      mutationInFlight.current = false
      if (connection.active) setBusy(false)
    }
  }

  const previewForget = async () => {
    if (locked || !detail) return
    const revision = ++reads.current
    setLoading(true); setError(''); setReceipt(''); setForgetPreview(null)
    try {
      const result = await connection.request('memory.forget_preview', {
        memory_id: detail.memory.memory_id, expected_version: detail.memory.row_version,
      })
      if (!connection.active || revision !== reads.current) return
      const preview = result.preview as MemoryForgetPreview | undefined
      // Every destructive target must have been shown, including its reviewed version.
      if (!preview || !Array.isArray(preview.targets) || !Array.isArray(preview.memories) || !preview.targets.length
        || !preview.targets.some(target => target.memory_id === detail.memory.memory_id && target.row_version === detail.memory.row_version)
        || !preview.targets.every(target => preview.memories.some(item => item.memory_id === target.memory_id && item.row_version === target.row_version))) {
        throw new Error('Incomplete forget preview')
      }
      setForgetPreview(preview)
    } catch (failure) {
      if (connection.active && revision === reads.current) setError(memoryErrorText(failure))
    } finally { if (connection.active && revision === reads.current) setLoading(false) }
  }

  const save = () => {
    if (locked || !text.trim() || source && !span) return
    const params = { text, ...(span ? { source: span } : {}) }
    void perform(editor === 'correct' && detail
      ? memoryAttempt('memory.correct', { ...params, memory_id: detail.memory.memory_id,
        expected_version: detail.memory.row_version, mode: correctionMode })
      : memoryAttempt('memory.remember', { ...params, scope }))
  }

  const scopeName = (value: MemoryScope) => value === 'shared' ? t('Shared with my characters')
    : value === 'project' ? t('Current character and Project') : t('Current character')

  const decideSuggestion = (decision: 'save' | 'dismiss') => {
    if (locked || !reviewTarget || (decision === 'save' && !suggestion?.savable)) return
    void perform(memoryAttempt('memory.review_candidate', {
      candidate_id: reviewTarget.candidate_id, expected_version: reviewTarget.row_version, decision,
      ...(decision === 'save' ? { kind: suggestion!.kind } : {}),
    }))
  }

  return <section hidden={!visible} className="memory-panel" aria-label={t('Memory')}>
    <header className="memory-header">
      <button type="button" className="icon-button" onClick={onClose} aria-label={t('Back to chat')}>
        <FluentIcon name="LeftArrow" size={16} />
      </button>
      <div className="memory-header-copy">
        <h2 className="settings-panel-title">{t('Memory')}</h2>
        <p className="settings-panel-description">{characterName}{projectName ? ` · ${projectName}` : ''}</p>
      </div>
      <SettingsButton size="small" disabled={busy || loading} onClick={() => {
        if (!pending) { setDetail(null); setEditor('inspect'); setForgetPreview(null) }
        void load(scope)
      }}>
        <FluentIcon name="Sync" size={14} />{t('Refresh')}
      </SettingsButton>
    </header>
    <div className="memory-scroll">
      <p className="memory-note">{t('Save and manage explicit memories here. Deleting a chat does not delete its saved memories.')}</p>
      {loading && <p role="status" className="memory-note">{t('Loading memories…')}</p>}
      {error && <p role="alert" className="memory-feedback" data-tone="danger">{t(error)}</p>}
      {receipt && <p role="status" className="memory-feedback">{t(receipt)}</p>}
      {pending && !busy && <div className="memory-retry">
        <p className="memory-note">{t('The outcome is unconfirmed. Retry uses the same operation and cannot create a duplicate memory.')}</p>
        <SettingsButton onClick={() => { void perform(pending) }} disabled={!connection.context}>{t('Retry same operation')}</SettingsButton>
      </div>}
      {!loading && !enabled && !error && <p className="memory-note">{t('Memory is disabled in this backend.')}</p>}
      {(status.source_policy_state === 'recovery_required'
        || ['sidecar_rebuild_required', 'publication_pending'].includes(status.source_policy_reason || '')) && <div
        role={status.source_policy_state === 'recovery_required' ? 'alert' : 'status'}
        className="memory-feedback" data-tone={status.source_policy_state === 'recovery_required' ? 'danger' : undefined}>
        <p>{t(status.source_policy_state === 'recovery_required'
          ? 'Memory source protections need recovery before chat history can be used.'
          : 'Memory source protections are active. Protected chat history remains usable.')}</p>
        {memorySourcePolicyReasonText(status.source_policy_reason) && <p>{t(memorySourcePolicyReasonText(status.source_policy_reason))}</p>}
        {status.source_policy_state === 'ready' && status.source_policy_reason === 'publication_pending'
          ? <p>{t('Refresh shortly to check memory source protections.')}</p>
          : <>
            <p>{t(memorySourcePolicyActionText(status.source_policy_reason))}</p>
            {['invalid_coordinates', 'sidecar_rebuild_required', 'publication_pending'].includes(status.source_policy_reason || '')
              && <code>{memoryPolicyRecoveryCommand}</code>}
          </>}
      </div>}
      {enabled && <>
        <div className="memory-status" role="status">
          <StatusPill ok={status.semantic_state === 'ready'}>{t(memoryIndexText(status.semantic_state))}</StatusPill>
          {memoryAutomaticAdmissionText(status.automatic_admission) && <StatusPill ok={status.automatic_admission === 'ready'}>
            {t(memoryAutomaticAdmissionText(status.automatic_admission))}
          </StatusPill>}
          {status.cleanup_state === 'pending' && <StatusPill ok={false}>{t('Storage cleanup pending')}</StatusPill>}
          {status.cleanup_state === 'unknown' && <StatusPill ok={false}>{t('Storage cleanup status unknown')}</StatusPill>}
        </div>
        {memorySemanticReasonText(status.semantic_reason) && <p className="memory-note">{t(memorySemanticReasonText(status.semantic_reason))}</p>}
        {status.automatic_admission === 'ready' && <p className="memory-note">{t('Automatic recall is experimental and may include unrelated memories.')}</p>}
        {reviewAvailable && <div className="memory-actions">
          <SettingsButton size="small" tone={showSuggestions ? 'secondary' : 'primary'} aria-pressed={!showSuggestions}
            disabled={locked || editing} onClick={() => { setShowSuggestions(false); setReceipt('') }}>{t('Saved memories')}</SettingsButton>
          <SettingsButton size="small" tone={showSuggestions ? 'primary' : 'secondary'} aria-pressed={showSuggestions}
            disabled={locked || editing} onClick={() => { setShowSuggestions(true); setDetail(null); setReceipt('') }}>{t('Review suggestions')}</SettingsButton>
        </div>}
        {showSuggestions ? <>
          <p className="memory-note">{t('Check the quoted messages and any conditions before saving.')}</p>
          {!loading && suggestions.length === 0 && <p className="memory-empty">{t('No memory suggestions to review.')}</p>}
          <div className="memory-list" aria-label={t('Review suggestions')}>
            {suggestions.map(item => <button key={item.candidate_id} className="memory-list-item" type="button"
              aria-pressed={reviewTarget?.candidate_id === item.candidate_id} disabled={locked}
              onClick={() => { void inspectSuggestion(item) }}>
              <span className="memory-list-text">{t(suggestionKindText(item.kind))}</span>
              <span className="memory-note">{scopeName(item.scope)} · {new Date(item.created_at_ms).toLocaleString()}</span>
            </button>)}
          </div>
          {reviewTarget && <CardShell vertical>
            <h3 className="settings-group-title">{t(suggestionKindText(reviewTarget.kind))}</h3>
            {suggestion && <>
              <p className="memory-body">{suggestion.text}</p>
              {suggestion.condition && <p className="memory-note">{t('Applies when {condition}', { condition: suggestion.condition })}</p>}
              {suggestion.quotes.map((quote, index) => <div key={index}>
                <p className="memory-note">{t(quote.speaker_role === 'user' ? 'Quote from your message' : 'Quote from the character’s message')}</p>
                <pre className="memory-source-preview">{quote.text}</pre>
              </div>)}
            </>}
            <div className="memory-actions">
              {suggestion?.savable && <SettingsButton tone="primary" disabled={locked}
                onClick={() => decideSuggestion('save')}>{t('Save as memory')}</SettingsButton>}
              <SettingsButton disabled={locked} onClick={() => decideSuggestion('dismiss')}>{t('Dismiss suggestion')}</SettingsButton>
            </div>
          </CardShell>}
        </> : <>
        <p className="memory-note">{t('Semantic indexing finds candidates; it does not establish that a memory is relevant.')}</p>
        <p className="memory-note">{t('A durable save can finish before semantic indexing. Lexical recall alone does not provide full semantic recall.')}</p>
        <div className="memory-tools">
          <label className="settings-field-label" htmlFor="memory-scope">{t('Scope')}</label>
          <select id="memory-scope" className="settings-form-input" value={scope} disabled={locked || editing}
            onChange={event => {
              ++listGeneration.current
              setScope(event.target.value as MemoryScope); setDetail(null); setReceipt('')
            }}>
            {(['character', 'shared', 'project'] as MemoryScope[]).map(kind => <option key={kind} value={kind}
              disabled={!scopes.some(option => option.kind === kind && option.available)}>{scopeName(kind)}</option>)}
          </select>
          <SettingsButton size="small" disabled={locked || editing || !scopes.some(option => option.kind === scope && option.available)} onClick={() => begin('new')}>
            <FluentIcon name="Add" size={14} />{t('New memory')}
          </SettingsButton>
        </div>
        {scope === 'shared' && <p className="memory-note">{t('Shared memories are explicitly available across your characters. Other scopes stay with the current character.')}</p>}
        {scope === 'project' && <p className="memory-note">{t('Project memories stay with this character in the current Project.')}</p>}

        {editing ? <CardShell vertical>
          <h3 className="settings-group-title">{t(editor === 'new' ? 'New memory' : 'Correct memory')}</h3>
          {editor === 'correct' && <SettingsField id="memory-correction-mode" label="Reason for correction">
            <select id="memory-correction-mode" className="settings-form-input" value={correctionMode} disabled={locked}
              onChange={event => setCorrectionMode(event.target.value as 'change' | 'error')}>
              <option value="change">{t('A real change — supersede the old value')}</option>
              <option value="error">{t('The old record was wrong — retract it')}</option>
            </select>
          </SettingsField>}
          {editor === 'correct' && <p className="memory-note">{t('A correction keeps the original scope. Its previous value is labelled in history and excluded from normal recall.')}</p>}
          <SettingsField id="memory-text" label="Memory text">
            <textarea id="memory-text" className="settings-form-input memory-text" rows={5} value={text} disabled={locked}
              onChange={event => setText(event.target.value)} />
          </SettingsField>
          <SettingsField id="memory-source" label="Optional committed source" description="Manual entries have their own save receipt. No chat message is attached unless you choose one.">
            <select id="memory-source" className="settings-form-input" value={sourceIndex} disabled={locked}
              onChange={event => {
                const index = Number(event.target.value); setSourceIndex(index)
                setRange({ start: 0, end: 0 }); setSelection(null)
              }}>
              <option value={-1}>{t('No source message')}</option>
              {sources.map((item, index) => <option key={`${item.message_id}:${item.revision}`} value={index}>
                {t(item.role === 'assistant' ? 'Character' : 'You')} · {Array.from(item.text).slice(0, 65).join('')}
              </option>)}
            </select>
          </SettingsField>
          {!sourcesAvailable && <p className="memory-note">{t('Committed sources are unavailable. You can still save a manual memory.')}</p>}
          {source && <div className="memory-source">
            <p className="memory-note">{t('Select the part of this committed message that supports your memory, or use the whole message.')}</p>
            <textarea readOnly className="settings-form-input memory-source-text" rows={5} value={source.text}
              aria-label={t('Committed source message')} onSelect={event => {
                const chosen = selectedSourceSpan(source, event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
                setSelection(chosen ? { start: chosen.start_cp, end: chosen.end_cp } : null)
              }} />
            <div className="memory-actions">
              <SettingsButton size="small" disabled={locked || !selection} onClick={() => { if (selection) setRange(selection) }}>{t('Use selected text')}</SettingsButton>
              <SettingsButton size="small" disabled={locked} onClick={() => setRange({ start: 0, end: Array.from(source.text).length })}>{t('Use whole message')}</SettingsButton>
            </div>
            {span ? <div>
              <p className="memory-note">{t('Selected source text')}</p>
              <pre className="memory-source-preview">{sourceTextParts(source, range.start, range.end)[1]}</pre>
            </div> : <p className="memory-note">{t('Choose source text before saving, or select no source message.')}</p>}
          </div>}
          <div className="memory-actions">
            <SettingsButton tone="primary" disabled={locked || !text.trim() || Boolean(source && !span)} onClick={save}>{t(busy ? 'Saving…' : 'Save memory')}</SettingsButton>
            <SettingsButton disabled={locked} onClick={() => { setEditor('inspect'); setSourceIndex(-1) }}>{t('Cancel')}</SettingsButton>
          </div>
        </CardShell> : <>
          {!loading && items.length === 0 && <p className="memory-empty">{t('No saved memories in this scope.')}</p>}
          <div className="memory-list" aria-label={t('Saved memories')}>
            {items.map(item => <button key={item.memory_id} className="memory-list-item" type="button"
              aria-pressed={detail?.memory.memory_id === item.memory_id} disabled={locked}
              onClick={() => { void inspect(item.memory_id) }}>
              <span className="memory-list-text">{item.text}</span>
              <span className="memory-note">{t(memoryLifecycleText(item.state))} · {t(memoryIndexText(item.index_state))}</span>
            </button>)}
          </div>
          {nextCursor && <SettingsButton disabled={locked || pageLoading} onClick={() => { void loadMore() }}>
            {t(pageLoading ? 'Loading older memories…' : 'Load more memories')}
          </SettingsButton>}
          {detail && <CardShell vertical>
            <h3 className="settings-group-title">{t('Inspect memory')}</h3>
            <p className="memory-body">{detail.memory.text}</p>
            <p className="memory-note">{scopeName(detail.memory.scope.kind)} · {t(memoryLifecycleText(detail.memory.state))} · {t('Version {version}', { version: detail.memory.row_version })}</p>
            <StatusPill ok={detail.memory.index_state === 'ready'}>{t(memoryIndexText(detail.memory.index_state))}</StatusPill>
            <p className="memory-note">{t('Memory has its own save receipt. Associated source coordinates, when present, refer to committed chat revisions.')}</p>
            {(detail.memory.sources || (detail.memory.source ? [detail.memory.source] : [])).map((origin, index) => <p key={index} className="memory-coordinate">
              {t('Source {message}, revision {revision}, characters {start}–{end}', { message: origin.message_id,
                revision: origin.revision, start: origin.start_cp, end: origin.end_cp })}
            </p>)}
            {detail.history.length > 0 && <details className="memory-history">
              <summary>{t('Correction history')}</summary>
              {detail.history.map(item => <div key={item.memory_id} className="memory-history-item">
                <strong>{t(memoryLifecycleText(item.state))}</strong><p className="memory-body">{item.text}</p>
              </div>)}
            </details>}
            <div className="memory-actions">
              <SettingsButton disabled={locked || detail.memory.state !== 'active'} onClick={() => begin('correct')}>{t('Correct')}</SettingsButton>
              <SettingsButton tone="danger" disabled={locked} onClick={() => { void previewForget() }}>{t('Forget')}</SettingsButton>
            </div>
            {forgetPreview && <div className="memory-forget" role="group" aria-label={t('Confirm forgetting memory')}>
              <p>{t(forgetPreview.targets.length > 1
                ? 'Forget all listed memories and their correction history? Unlisted memories stay unchanged.'
                : 'Forget this memory and all its correction history? Other saved memories stay unchanged.')}</p>
              {forgetPreview.targets.length > 1 && <>
                <p>{t('These memories share overlapping source text. Forgetting also removes every listed linked memory and its history.')}</p>
                <ul className="memory-linked-list">{forgetPreview.memories.map(item => <li key={item.memory_id}>
                  <p className="memory-body">{item.text}</p><span className="memory-note">{scopeName(item.scope.kind)}</span>
                </li>)}</ul>
              </>}
              <p className="memory-note">{t('Known associated source spans will be excluded from future model context. The displayed chat remains; unrelated mentions and paraphrases are not automatically found.')}</p>
              <p className="memory-note">{t('Content already sent to an external model cannot be removed retroactively.')}</p>
              <div className="memory-actions">
                <SettingsButton tone="danger" disabled={locked} onClick={() => {
                  if (!locked) void perform(memoryAttempt('memory.forget', { memory_id: detail.memory.memory_id,
                    expected_version: detail.memory.row_version, confirmed_targets: forgetPreview.targets }))
                }}>{t(busy ? 'Forgetting…' : forgetPreview.targets.length > 1 ? 'Forget all listed memories and history' : 'Forget memory and history')}</SettingsButton>
                <SettingsButton disabled={locked} onClick={() => setForgetPreview(null)}>{t('Cancel')}</SettingsButton>
              </div>
            </div>}
          </CardShell>}
        </>}
        </>}
      </>}
    </div>
  </section>
}
