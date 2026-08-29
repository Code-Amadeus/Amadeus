import { useEffect, useState, type CSSProperties } from 'react'
import type { Page } from '../App'
import FluentIcon from './FluentIcon'

interface Props {
  page: Page
  onNavigate: (p: Page) => void
  renderActive: boolean
  wallpaperActive: boolean
  onToggleRender: () => void
  onToggleWallpaper: () => void
}

type NavItem =
  | { kind: 'page'; page: Page; label: string; icon: 'Edit' | 'Setting' | 'CommandPrompt' | 'Movie' }
  | { kind: 'toggle'; label: string; icon: 'Video'; iconActive: 'Movie'; active: boolean; onClick: () => void }
  | { kind: 'toggle-simple'; label: string; icon: 'Tiles'; active: boolean; onClick: () => void }

function navButtonStyle(active: boolean, collapsed: boolean): CSSProperties {
  return {
    height: 40,
    paddingLeft: collapsed ? (active ? 14 : 17) : (active ? 17 : 20),
    paddingRight: collapsed ? 0 : 20,
    color: active ? 'var(--accent)' : 'var(--muted)',
    backgroundColor: active ? 'var(--selection-bg)' : 'transparent',
    borderLeft: active ? '3px solid var(--accent)' : '3px solid transparent',
    fontWeight: active ? 600 : 400,
  }
}

function CollapseGlyph({ collapsed }: { collapsed: boolean }) {
  return <span className={`nav-collapse-glyph ${collapsed ? 'collapsed' : 'expanded'}`} />
}

export default function Sidebar({
  page, onNavigate,
  renderActive, wallpaperActive, onToggleRender, onToggleWallpaper,
}: Props) {
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
          title={collapsed ? item.label : undefined}
          className="flex items-center gap-3 w-full text-left text-[13px]
                     transition-colors duration-150"
          style={navButtonStyle(active, collapsed)}
          onMouseEnter={e => {
            if (!active) {
              (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--hover)'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--text)'
            }
          }}
          onMouseLeave={e => {
            if (!active) {
              (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--muted)'
            }
          }}
        >
          <FluentIcon name={item.icon} size={18} />
          {!collapsed && <span>{item.label}</span>}
        </button>
      )
    }
    // toggle items — selectable=false, show active state
    const iconName = item.kind === 'toggle' && item.active ? item.iconActive : item.icon
    return (
      <button
        key={item.label}
        onClick={item.onClick}
        title={collapsed ? item.label : undefined}
        className="flex items-center gap-3 w-full text-left text-[13px]
                   transition-colors duration-150"
        style={navButtonStyle(item.active, collapsed)}
        onMouseEnter={e => {
          if (!item.active) {
            (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--hover)'
            ;(e.currentTarget as HTMLElement).style.color = 'var(--text)'
          }
        }}
        onMouseLeave={e => {
          if (!item.active) {
            (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent'
            ;(e.currentTarget as HTMLElement).style.color = 'var(--muted)'
          }
        }}
      >
        <FluentIcon name={iconName} size={18} />
        {!collapsed && <span>{item.label}</span>}
      </button>
    )
  }

  return (
    <nav
      className="flex flex-col select-none shrink-0"
      style={{
        width: collapsed ? 56 : 220,
        backgroundColor: 'var(--bg)',
        border: 'none',
        transition: 'width 140ms ease',
      }}
    >
      <div
        className="flex items-center"
        style={{
          height: 44,
          padding: collapsed ? '7px 8px' : '7px 12px',
          justifyContent: collapsed ? 'center' : 'flex-end',
        }}
      >
        <button
          onClick={() => setCollapsed(v => !v)}
          title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          className="flex items-center justify-center border-none bg-transparent cursor-pointer"
          style={{
            width: 30,
            height: 30,
            borderRadius: 6,
            color: 'var(--muted)',
          }}
          onMouseEnter={e => {
            const b = e.currentTarget
            b.style.backgroundColor = 'var(--hover)'
            b.style.color = 'var(--text)'
          }}
          onMouseLeave={e => {
            const b = e.currentTarget
            b.style.backgroundColor = 'transparent'
            b.style.color = 'var(--muted)'
          }}
        >
          <CollapseGlyph collapsed={collapsed} />
        </button>
      </div>

      <div className="flex-1" style={{ paddingTop: 0 }}>
        {topItems.map(renderItem)}
      </div>

      <div style={{ paddingTop: 6, paddingBottom: 8, borderTop: '1px solid rgba(17,24,39,0.07)' }}>
        {bottomItems.map(renderItem)}
      </div>
    </nav>
  )
}
