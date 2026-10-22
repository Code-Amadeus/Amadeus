import { useEffect, useState } from 'react'
import type { Page } from '../App'
import FluentIcon from './FluentIcon'
import { useI18n } from '../i18n'

interface Props {
  page: Page
  onNavigate: (p: Page) => void
  renderActive: boolean
  wallpaperActive: boolean
  onToggleRender: () => void
  onToggleWallpaper: () => void
}

type NavItem =
  | { kind: 'page'; page: Page; label: string; icon: 'Edit' | 'Setting' | 'CommandPrompt' | 'Movie' | 'Tiles' }
  | { kind: 'toggle'; label: string; icon: 'Video'; iconActive: 'Movie'; active: boolean; onClick: () => void }
  | { kind: 'toggle-simple'; label: string; icon: 'Tiles'; active: boolean; onClick: () => void }

function CollapseGlyph({ collapsed }: { collapsed: boolean }) {
  return <span className={`nav-collapse-glyph ${collapsed ? 'collapsed' : 'expanded'}`} />
}

export default function Sidebar({
  page, onNavigate,
  renderActive, wallpaperActive, onToggleRender, onToggleWallpaper,
}: Props) {
  const { t } = useI18n()
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('amadeus.sidebar.collapsed') === '1')

  useEffect(() => {
    localStorage.setItem('amadeus.sidebar.collapsed', collapsed ? '1' : '0')
  }, [collapsed])

  const isActive = (p: Page) => page === p

  const topItems: NavItem[] = [
    { kind: 'page', page: 'chat', label: 'Chat', icon: 'Edit' },
    {
      kind: 'toggle',
      label: 'Render',
      icon: 'Video',
      iconActive: 'Movie',
      active: renderActive,
      onClick: onToggleRender,
    },
    {
      kind: 'toggle-simple',
      label: 'Wallpaper',
      icon: 'Tiles',
      active: wallpaperActive,
      onClick: onToggleWallpaper,
    },
    { kind: 'page', page: 'vn', label: 'VN Player', icon: 'Movie' },
    // Keep experimental plugin management in Settings until integration is ready.
  ]

  const bottomItems: NavItem[] = [
    { kind: 'page', page: 'backend', label: 'Backend', icon: 'CommandPrompt' },
    { kind: 'page', page: 'settings', label: 'Settings', icon: 'Setting' },
  ]

  const renderItem = (item: NavItem) => {
    if (item.kind === 'page') {
      const active = isActive(item.page)
      return (
        <button
          key={item.page}
          onClick={() => onNavigate(item.page)}
          title={collapsed ? t(item.label) : undefined}
          className="sidebar-item"
          data-active={active || undefined}
          aria-current={active ? 'page' : undefined}
        >
          <FluentIcon name={item.icon} size={16} />
          {!collapsed && <span>{t(item.label)}</span>}
        </button>
      )
    }
    // toggle items — selectable=false, show active state
    const iconName = item.kind === 'toggle' && item.active ? item.iconActive : item.icon
    return (
      <button
        key={item.label}
        onClick={item.onClick}
        title={collapsed ? t(item.label) : undefined}
        className="sidebar-item"
        data-active={item.active || undefined}
        aria-pressed={item.active}
      >
        <FluentIcon name={iconName} size={16} />
        {!collapsed && <span>{t(item.label)}</span>}
      </button>
    )
  }

  return (
    <nav className="sidebar" data-collapsed={collapsed || undefined}>
      <div className="sidebar-header">
        <button
          onClick={() => setCollapsed(v => !v)}
          title={t(collapsed ? 'Expand navigation' : 'Collapse navigation')}
          className="icon-button"
        >
          <CollapseGlyph collapsed={collapsed} />
        </button>
      </div>

      <div className="flex-1">
        {topItems.map(renderItem)}
      </div>

      <div className="sidebar-footer">
        {bottomItems.map(renderItem)}
      </div>
    </nav>
  )
}
