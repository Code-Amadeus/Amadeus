import { useEffect, useState } from 'react'
import RichCard from './RichCard'
import type { WorkPageProps } from './types'

interface Props {
  workItemId: string
  runId: string
  recipient: string
  enabled: boolean
  inputs: Record<string, unknown>[]
  send: WorkPageProps['send']
  subscribe: WorkPageProps['subscribe']
}

function keepReceipt(previous: Record<string, unknown>[], receipt: Record<string, unknown>) {
  const index = previous.findIndex(row => row.input_id === receipt.input_id)
  if (index < 0) return [...previous, receipt]
  if (previous[index].state !== 'unknown' && receipt.state === 'unknown') return previous
  return previous.map((row, i) => i === index ? receipt : row)
}

export default function ProviderInputComposer({ workItemId, runId, recipient, enabled, inputs, send, subscribe }: Props) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [submitted, setSubmitted] = useState<Record<string, unknown>[]>([])
  const [error, setError] = useState('')
  useEffect(() => subscribe('work.input.updated', payload => {
    const receipt = payload.input as Record<string, unknown> | undefined
    if (receipt?.work_item_id === workItemId && receipt.provider_run_id === runId) {
      setSubmitted(previous => keepReceipt(previous, receipt))
    }
  }), [subscribe, workItemId, runId])
  const receipts = new Map(submitted.map(input => [String(input.input_id), input]))
  for (const input of inputs.filter(row => row.provider_run_id === runId)) {
    const key = String(input.input_id)
    if (!receipts.has(key) || input.state !== 'unknown' || receipts.get(key)?.state === 'unknown') {
      receipts.set(key, input)
    }
  }

  async function submit() {
    if (busy || !enabled || !text.trim()) return
    const input = { input_id: crypto.randomUUID(), work_item_id: workItemId, run_id: runId, text }
    setBusy(true)
    setText('')
    setError('')
    // The exact recipient and original text are retained even if selection
    // changes or the response is lost. There is no automatic resend.
    setSubmitted(previous => [...previous, { ...input, state: 'unknown' }])
    try {
      const response = await send('work.input', input)
      const receipt = response.input
      if (receipt && typeof receipt === 'object' && !Array.isArray(receipt)) {
        setSubmitted(previous => keepReceipt(previous, receipt as Record<string, unknown>))
      } else {
        setError(String(response.error || 'Delivery could not be confirmed.'))
      }
    } catch {
      setError('Delivery could not be confirmed. Check the saved message before sending it again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <RichCard type="message" title={`Message ${recipient}`}>
      <div className="crt-provider-input">
      <p>Send additional input for this running work.</p>
      {Array.from(receipts.values()).map(input => (
        <div key={String(input.input_id)}>
          <p style={{ whiteSpace: 'pre-wrap' }}>{String(input.text || '')}</p>
          <small>{input.state === 'delivered' ? 'Received by provider'
            : input.state === 'rejected' ? 'Not delivered' : 'Delivery unconfirmed'}</small>
        </div>
      ))}
      <textarea aria-label={`Message ${recipient}`} value={text} disabled={!enabled || busy}
        onChange={event => setText(event.target.value)} placeholder="Add a fact, ask a question, or clarify the current work" />
      <button onClick={() => { void submit() }} disabled={!enabled || busy || !text.trim()}>
        {busy ? 'Sending…' : 'Send message'}
      </button>
      {!enabled && <p>This run is not available for additional messages.</p>}
      {error && <p role="status">{error}</p>}
      </div>
    </RichCard>
  )
}
