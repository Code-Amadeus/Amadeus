import type { ProjectStateMap, WorkTurnNode } from './types'

interface Props {
  map: ProjectStateMap
  onSelectTurn: (turnId: string) => void
}

function nodeClass(node: WorkTurnNode, currentTurnId: string): string {
  const classes = ['crt-map-node', node.status, node.validation]
  if (node.id === currentTurnId) classes.push('current')
  return classes.join(' ')
}

export default function ProjectStateMapView({ map, onSelectTurn }: Props) {
  return (
    <section className="crt-project-map">
      <div className="crt-project-map-head">
        <span>Project State Map</span>
        <small>{map.turns.length} turns</small>
      </div>
      <div className="crt-map-track">
        {map.turns.map((node, index) => (
          <div className="crt-map-item" key={node.id}>
            {index > 0 && <i className="crt-map-edge" />}
            <button className={nodeClass(node, map.currentTurnId)} onClick={() => onSelectTurn(node.id)}>
              <span className="crt-map-dot" />
              <strong>{node.title || `Turn ${index + 1}`}</strong>
              <small>
                {node.provider} · {node.changedFiles} files · {node.validation}
              </small>
              {node.artifactCount > 0 && <em>{node.artifactCount} artifacts</em>}
            </button>
          </div>
        ))}
      </div>
    </section>
  )
}
