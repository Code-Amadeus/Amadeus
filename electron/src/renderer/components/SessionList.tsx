interface Session {
  id: string
  title: string
}

interface Props {
  sessions: Session[]
  activeId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  onDelete: (id: string) => void
}

export default function SessionList({ sessions, activeId, onSelect, onNew, onDelete }: Props) {
  return (
    <div className="bg-[#FAFAFA] border-b border-[#E8E8E8] shrink-0">
      {/* header */}
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-[11px] font-medium text-[#888888] uppercase tracking-wide">Sessions</span>
        <button
          onClick={onNew}
          className="text-[11px] text-[#0078D4] hover:text-[#005A9E] font-medium"
        >
          + New
        </button>
      </div>

      {/* list */}
      {sessions.length === 0 ? (
        <div className="px-3 pb-2 text-[11px] text-[#AAAAAA] italic">No sessions yet</div>
      ) : (
        <div className="max-h-[120px] overflow-y-auto">
          {sessions.map(s => (
            <div
              key={s.id}
              onClick={() => onSelect(s.id)}
              className={`group flex items-center justify-between px-3 py-1.5 text-[13px] cursor-pointer
                ${s.id === activeId ? 'bg-[#EAEAEA] text-[#333333]' : 'text-[#555555] hover:bg-[#F0F0F0]'}
                border-b border-[#F0F0F0] last:border-b-0 rounded mx-1 my-0.5`}
            >
              <span className="truncate">{s.title}</span>
              <button
                onClick={e => { e.stopPropagation(); onDelete(s.id) }}
                className="opacity-0 group-hover:opacity-100 text-[#AAAAAA] hover:text-[#C42B1C] text-xs px-1"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
