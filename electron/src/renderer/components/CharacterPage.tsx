import type { ReactNode, KeyboardEvent } from 'react'
import { useI18n } from '../i18n'
import { useActiveCharacter } from '../activeCharacter'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import type { CharacterSection } from './characterWorkspace'
import '../styles/characterWorkspace.css'

const sections: { id: CharacterSection; label: string; icon: FluentIconName; description: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'Tiles', description: 'Start with a personality. Keep the appearance and voice you already use.' },
  { id: 'identity', label: 'Identity & persona', icon: 'People', description: 'Create personalities and choose who joins your next conversation.' },
  { id: 'appearance', label: 'Appearance', icon: 'Palette', description: 'Choose the artwork shared by Render, Wallpaper and your roles.' },
  { id: 'knowledge', label: 'Knowledge & memory', icon: 'Album', description: 'Review reference knowledge and how conversations are kept.' },
]

export default function CharacterPage({ panels, connected, section, onSectionChange, voiceSummary, onOpenVoice }: {
  connected: boolean; section: CharacterSection; onSectionChange: (section: CharacterSection) => void;
  panels: Record<Exclude<CharacterSection, 'overview'>, ReactNode>; voiceSummary: string; onOpenVoice: () => void;
}) {
  const { t } = useI18n()
  const active = useActiveCharacter()
  const current = sections.find(value => value.id === section)!
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number
    if (event.key === 'ArrowRight') next = (index + 1) % sections.length
    else if (event.key === 'ArrowLeft') next = (index + sections.length - 1) % sections.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = sections.length - 1
    else return
    event.preventDefault()
    onSectionChange(sections[next].id)
    document.getElementById(`character-tab-${sections[next].id}`)?.focus()
  }
  const cards: { id: string; title: string; description: string; scope: string; icon: FluentIconName; action: string; open: () => void }[] = [
    { id: 'identity', title: 'Identity & persona', description: 'Create a role with a name and personality. Switching roles takes effect after a backend restart.', scope: 'Role identity', icon: 'People', action: 'Manage roles', open: () => onSectionChange('identity') },
    { id: 'appearance', title: 'Appearance', description: 'Sprite or Live2D, chat avatars and installed visual resources. Reuse the same artwork across personalities.', scope: 'Shared by all roles', icon: 'Palette', action: 'Manage appearance', open: () => onSectionChange('appearance') },
    { id: 'voice', title: 'Voice', description: 'Models, reference audio, recognition and speech services stay together in Settings → Voice.', scope: 'Shared by all roles', icon: 'Microphone', action: 'Open voice settings', open: onOpenVoice },
    { id: 'knowledge', title: 'Knowledge & memory', description: 'Conversations stay with their role. Review the existing reference library and shared project boundaries.', scope: 'Existing knowledge and history', icon: 'Album', action: 'Review knowledge & history', open: () => onSectionChange('knowledge') },
  ]
  return <div className="character-workspace">
    <header className="character-workspace-header">
      <div className="character-workspace-title"><h3>{t('Characters')}</h3><p>{t('Personality, appearance and voice in one place.')}</p></div>
      <span className="character-active-label">{t('Active role')}<strong>{active?.ui_name || t(connected ? 'Loading active role…' : 'Backend not connected')}</strong></span>
    </header>
    <div className="character-workspace-tabs" role="tablist" aria-label={t('Character sections')}>
      {sections.map((item, index) => <button type="button" role="tab" key={item.id}
        id={`character-tab-${item.id}`} aria-selected={section === item.id} aria-controls={`character-panel-${item.id}`}
        tabIndex={section === item.id ? 0 : -1} onClick={() => onSectionChange(item.id)} onKeyDown={event => move(event, index)}>
        <FluentIcon name={item.icon} size={15} /><span>{t(item.label)}</span>
      </button>)}
    </div>
    <section className={`character-workspace-panel character-panel-${section}`} role="tabpanel" tabIndex={0}
      id={`character-panel-${section}`} aria-labelledby={`character-tab-${section}`}>
      <div className="character-section-intro"><h4>{t(current.label)}</h4><p>{t(current.description)}</p></div>
      {section === 'appearance' && <div className="character-shared-note">
        <FluentIcon name="People" size={16} /><span>{t('These settings apply to the application. Creating or changing a role does not require new artwork or a new voice.')}</span>
      </div>}
      {section === 'overview' ? <div className="character-overview-grid">
        {cards.map(card => <article key={card.id} className="character-overview-card">
          <div className="character-card-top"><span className="character-card-icon"><FluentIcon name={card.icon} size={20} /></span><span>{t(card.scope)}</span></div>
          <h4>{t(card.title)}</h4><p>{t(card.description)}</p>
          {card.id === 'voice' && voiceSummary ? <small>{t('Saved speech engine')}: {t(voiceSummary)}</small> : null}
          <button type="button" className="character-card-link" onClick={card.open}>{t(card.action)}<FluentIcon name="RightArrow" size={14} /></button>
        </article>)}
      </div> : <div className="character-panel-content">{panels[section]}</div>}
    </section>
  </div>
}
