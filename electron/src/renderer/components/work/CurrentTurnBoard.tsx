import type { WorkSignal, WorkTurn } from './types'

interface Props {
  turn: WorkTurn
  onAccept: () => void
  onOpenDiff: () => void
  onOpenTrace: () => void
  onRefine: () => void
}

function phaseLabel(phase: WorkTurn['phase']): string {
  return phase.charAt(0).toUpperCase() + phase.slice(1)
}

function importantSignals(signals: WorkSignal[]): WorkSignal[] {
  const priority = { blocking: 0, important: 1, normal: 2, ambient: 3 }
  return [...signals]
    .sort((a, b) => priority[a.importance] - priority[b.importance])
    .slice(0, 5)
}

export default function CurrentTurnBoard({
  turn,
  onAccept,
  onOpenDiff,
  onOpenTrace,
  onRefine,
}: Props) {
  const deltaSignals = turn.signals.filter(signal => signal.phase === 'edit' || signal.phase === 'result')
  const validation = turn.validation.slice(0, 3)
  const risks = turn.risks.slice(0, 3)
  const signals = importantSignals(turn.signals)

  return (
    <div className="crt-workboard">
      <div className="crt-workboard-header">
        <div>
          <span>Current Turn</span>
          <h1>{turn.title}</h1>
          <p>{turn.intent}</p>
        </div>
        <div className={`crt-turn-status ${turn.status}`}>
          <b>{turn.status}</b>
          <small>{phaseLabel(turn.phase)}</small>
        </div>
      </div>

      <div className="crt-workboard-grid">
        <section className="crt-workboard-card intent">
          <span>Intent</span>
          <p>{turn.summary || turn.intent}</p>
        </section>

        <section className="crt-workboard-card">
          <span>Delta Digest</span>
          {deltaSignals.length > 0 ? (
            <ul>
              {deltaSignals.slice(0, 4).map(signal => <li key={signal.id}>{signal.summary}</li>)}
            </ul>
          ) : (
            <p>No code delta has been observed yet.</p>
          )}
        </section>

        <section className="crt-workboard-card">
          <span>Validation</span>
          <ul>
            {validation.map(item => (
              <li key={item.id}>
                <b>{item.status}</b> {item.summary}
              </li>
            ))}
          </ul>
        </section>

        <section className="crt-workboard-card">
          <span>Watchpoints</span>
          <ul>
            {risks.map(risk => (
              <li key={risk.id}>
                <b>{risk.level}</b> {risk.summary}
              </li>
            ))}
          </ul>
        </section>
      </div>

      <div className="crt-signal-strip">
        {signals.length > 0 ? signals.map(signal => (
          <button key={signal.id} className={`crt-signal-card ${signal.importance}`} onClick={onOpenTrace}>
            <span>{signal.phase}</span>
            <strong>{signal.title}</strong>
            <p>{signal.summary}</p>
          </button>
        )) : (
          <div className="crt-signal-empty">Work signals will appear here after the provider starts.</div>
        )}
      </div>

      <div className="crt-workboard-actions">
        <button onClick={onAccept}>Accept</button>
        <button onClick={onOpenDiff}>Open diff</button>
        <button onClick={onOpenTrace}>View trace</button>
        <button onClick={onRefine}>Ask refinement</button>
      </div>
    </div>
  )
}
