import { useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import { useI18n } from '../i18n'
import {
  type CapabilityProfile,
  type CapabilitySceneUsage,
  type SceneCapabilityImportance,
  type SceneCapabilityState,
  type SceneConfigureSection,
  type SceneId,
} from './sceneCapabilityProjection'

interface Props {
  capabilities: CapabilityProfile[]
  runtimePackages: RuntimePackageStatus[]
  onOpenSection: (section: SceneConfigureSection, targetId?: string) => void
}

export interface RuntimePackageStatus {
  id: string
  label: string
  description: string
  value: string
  state: SceneCapabilityState
  stateLabel: string
  icon: FluentIconName
}

type CapabilityPage = 'overview' | 'scene_status' | 'runtime_packages'
type CapabilityCategory = CapabilityProfile['category']

const CATEGORY_ORDER: CapabilityCategory[] = [
  'Conversation roles',
  'Work',
  'Application',
  'Voice',
  'Perception',
  'Presentation',
]

const CATEGORY_DESCRIPTIONS: Record<CapabilityCategory, string> = {
  'Conversation roles': 'Primary and scene-specific conversational responsibilities',
  Work: 'Delegated task execution',
  Application: 'Host-governed interaction with applications',
  Voice: 'Speech input, output, and realtime audio behavior',
  Perception: 'Shared visual understanding and capture paths',
  Presentation: 'Translation and user-facing output transforms',
}

const CAPABILITY_ICONS: Record<string, FluentIconName> = {
  main_conversation: 'Robot',
  vn_companion: 'Movie',
  work_execution: 'Work',
  application_interaction: 'CommandPrompt',
  speech_output: 'Play',
  conversation_recognition: 'Microphone',
  wake_recognition: 'Microphone',
  echo_cancellation: 'Tiles',
  voice_interruption: 'Microphone',
  visual_understanding: 'Camera',
  translation: 'Language',
}

const SCENE_ORDER: SceneId[] = ['chat', 'wallpaper', 'vn']
const IMPORTANCE_ORDER: SceneCapabilityImportance[] = ['core', 'expected', 'optional']

const IMPORTANCE_LABELS: Record<SceneCapabilityImportance, string> = {
  core: 'Core',
  expected: 'Expected',
  optional: 'Optional',
}

const IMPORTANCE_DESCRIPTIONS: Record<SceneCapabilityImportance, string> = {
  core: 'Required for this scene',
  expected: 'Included in the scene preset',
  optional: 'Available when enabled',
}

const SCENE_LABELS: Record<SceneId, string> = {
  chat: 'Chat window',
  wallpaper: 'Wallpaper',
  vn: 'Visual Novel',
}

const STATE_LABELS: Record<SceneCapabilityState, string> = {
  ready: 'Ready',
  attention: 'Needs setup',
  inactive: 'Inactive',
  unknown: 'Unknown',
}

function statusLabel(state: SceneCapabilityState, explicit?: string): string {
  return explicit || STATE_LABELS[state]
}

function compactStatusLabel(state: SceneCapabilityState): string {
  if (state === 'attention') return 'Needs attention'
  if (state === 'inactive') return 'Off'
  if (state === 'unknown') return 'Unverified'
  return 'Ready'
}

function StatusBadge({ state, label }: { state: SceneCapabilityState; label?: string }) {
  const { t } = useI18n()
  return (
    <span className="capability-status-badge" data-state={state}>
      <span aria-hidden="true" />
      {t(statusLabel(state, label))}
    </span>
  )
}

function StatusDot({ state }: { state: SceneCapabilityState }) {
  const { t } = useI18n()
  const label = t(compactStatusLabel(state))
  return <span className="capability-status-dot" data-state={state} aria-label={label} title={label} />
}

function CapabilityIdentity({ capability }: { capability: CapabilityProfile }) {
  const { t } = useI18n()
  return (
    <div className="capability-identity">
      <span className="capability-icon" aria-hidden="true">
        <FluentIcon name={CAPABILITY_ICONS[capability.id] || 'Tiles'} size={16} />
      </span>
      <span>
        <strong>{t(capability.label)}</strong>
        <small>{t(capability.description)}</small>
      </span>
    </div>
  )
}

function ConfigureButton({
  section,
  actionLabel,
  targetId,
  onOpenSection,
}: {
  section?: SceneConfigureSection
  actionLabel?: string
  targetId?: string
  onOpenSection: (section: SceneConfigureSection, targetId?: string) => void
}) {
  const { t } = useI18n()
  if (!section) return <span className="capability-no-action">—</span>
  const label = t(actionLabel || `Open ${section} settings`)
  return (
    <button
      type="button"
      className="capability-configure-button"
      onClick={() => onOpenSection(section, targetId)}
      aria-label={label}
      title={label}
    >
      <FluentIcon name="Setting" size={14} />
    </button>
  )
}

function overviewConfigureTarget(capability: CapabilityProfile): CapabilitySceneUsage | undefined {
  return capability.scenes.find(scene => scene.used && scene.configureSection)
}

function Overview({ capabilities, onOpenSection }: Props) {
  const { t } = useI18n()
  const grouped = useMemo(() => CATEGORY_ORDER.map(category => ({
    category,
    capabilities: capabilities.filter(capability => capability.category === category),
  })).filter(group => group.capabilities.length > 0), [capabilities])

  return (
    <div className="capability-category-list">
      {grouped.map(group => (
        <section className="capability-category" key={group.category}>
          <header className="capability-category-header">
            <span>
              <strong>{t(group.category)}</strong>
              <small>{t(CATEGORY_DESCRIPTIONS[group.category])}</small>
            </span>
            <span>{group.capabilities.length} {t(group.capabilities.length === 1 ? 'capability' : 'capabilities')}</span>
          </header>
          <div>
            {group.capabilities.map(capability => {
              const target = overviewConfigureTarget(capability)
              return (
                <div className="capability-summary-row" key={capability.id}>
                  <CapabilityIdentity capability={capability} />
                  <div className="capability-implementation">
                    <span>{t('Implementation')}</span>
                    <strong>{t(capability.implementation)}</strong>
                  </div>
                  <div className="capability-row-actions">
                    <StatusBadge state={capability.state} label={capability.stateLabel} />
                    <ConfigureButton section={capability.configureSection || target?.configureSection} targetId={capability.id} onOpenSection={onOpenSection} />
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      ))}
    </div>
  )
}

function sceneCapabilities(capabilities: CapabilityProfile[], sceneId: SceneId) {
  return capabilities.flatMap(capability => {
    const usage = capability.scenes.find(scene => scene.sceneId === sceneId && scene.used)
    return usage ? [{ capability, usage }] : []
  })
}

function SceneStatus({ capabilities, onOpenSection }: Props) {
  const { t } = useI18n()
  const [expandedScenes, setExpandedScenes] = useState<Set<SceneId>>(() => new Set(SCENE_ORDER))
  const [diagnostic, setDiagnostic] = useState<{ capability: string; status: string; detail: string } | null>(null)
  const toggleScene = (sceneId: SceneId) => {
    setExpandedScenes(current => {
      const next = new Set(current)
      if (next.has(sceneId)) next.delete(sceneId)
      else next.add(sceneId)
      return next
    })
  }
  return (
    <div className="capability-scene-list">
      <div className="capability-scene-legend" aria-label={t('Capability status legend')}>
        {([
          ['ready', 'Ready'],
          ['attention', 'Needs attention'],
          ['inactive', 'Off'],
          ['unknown', 'Unverified'],
        ] as Array<[SceneCapabilityState, string]>).map(([state, label]) => (
          <span key={state}><StatusDot state={state} />{t(label)}</span>
        ))}
      </div>
      {diagnostic ? (
        <div className="capability-scene-diagnostic" role="status">
          <span>
            <strong>{t(diagnostic.capability)}</strong>
            <small>{t(diagnostic.status)} · {t(diagnostic.detail)}</small>
          </span>
          <button type="button" onClick={() => setDiagnostic(null)} aria-label={t('Close')}>×</button>
        </div>
      ) : null}
      {SCENE_ORDER.map(sceneId => {
        const registered = sceneCapabilities(capabilities, sceneId)
        const needsAttention = registered.filter(({ usage }) => usage.state === 'attention').length
        const unverified = registered.filter(({ usage }) => usage.state === 'unknown').length
        const backendDisconnected = registered.some(({ usage }) => usage.stateLabel === 'Backend not connected')
        const expanded = expandedScenes.has(sceneId)
        return (
          <section className="capability-scene-section" key={sceneId}>
            <button
              type="button"
              className="capability-scene-header"
              aria-expanded={expanded}
              aria-controls={`capability-scene-${sceneId}`}
              onClick={() => toggleScene(sceneId)}
            >
              <strong>{t(SCENE_LABELS[sceneId])}</strong>
              <span className="capability-scene-toggle-end">
                <span className="capability-scene-counts">
                  <b>{registered.length} {t('capabilities')}</b>
                  {backendDisconnected
                    ? <em data-unverified="true">{t('Backend not connected')}</em>
                    : needsAttention > 0
                      ? <em>{needsAttention} {t('need attention')}</em>
                      : unverified > 0
                        ? <em data-unverified="true">{t('{count} unverified', { count: unverified })}</em>
                        : <em data-ready="true">{t('Configuration complete')}</em>}
                </span>
                <span className="capability-scene-chevron" aria-hidden="true" />
              </span>
            </button>
            {expanded ? <div className="capability-scene-tiers" id={`capability-scene-${sceneId}`}>
              {IMPORTANCE_ORDER.map(importance => {
                const items = registered.filter(({ usage }) => usage.importance === importance)
                if (!items.length) return null
                return (
                  <div className="capability-scene-tier" data-importance={importance} key={importance}>
                    <div className="capability-scene-tier-heading">
                      <span>
                        <strong>{t(IMPORTANCE_LABELS[importance])}</strong>
                        <small>{t(IMPORTANCE_DESCRIPTIONS[importance])}</small>
                      </span>
                      <b>{items.length}</b>
                    </div>
                    <div className="capability-scene-grid">
                      {items.map(({ capability, usage }) => (
                        <div className="capability-scene-item" key={capability.id} aria-label={`${t(capability.label)}: ${t(statusLabel(usage.state, usage.stateLabel))}. ${t(usage.detail)}`}>
                          <div className="capability-scene-item-name">
                            <span aria-hidden="true">
                              <FluentIcon name={CAPABILITY_ICONS[capability.id] || 'Tiles'} size={14} />
                            </span>
                            <strong>{t(capability.label)}</strong>
                            <span className="sr-only">{t(usage.detail)}</span>
                          </div>
                          <span className="capability-scene-item-actions">
                            <button
                              type="button"
                              className="capability-status-inspect"
                              aria-label={`${t(capability.label)}: ${t(statusLabel(usage.state, usage.stateLabel))}. ${t(usage.detail)}`}
                              title={t('View status reason')}
                              onClick={() => setDiagnostic({
                                capability: capability.label,
                                status: statusLabel(usage.state, usage.stateLabel),
                                detail: usage.detail,
                              })}
                            >
                              <span className="capability-status-dot" data-state={usage.state} aria-hidden="true" />
                            </button>
                            {usage.configureSection ? <ConfigureButton section={usage.configureSection} actionLabel={usage.actionLabel} targetId={usage.id} onOpenSection={onOpenSection} /> : null}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div> : null}
          </section>
        )
      })}
    </div>
  )
}

function RuntimePackages({ runtimePackages }: Pick<Props, 'runtimePackages'>) {
  const { t } = useI18n()
  return (
    <div className="runtime-package-page">
      <div className="runtime-package-note">
        {t('Optional local resources detected by the current runtime. Package installation is managed outside Settings.')}
      </div>
      <div className="runtime-package-grid">
        {runtimePackages.map(packageStatus => (
          <article className="runtime-package-card" key={packageStatus.id}>
            <header>
              <span className="runtime-package-icon" aria-hidden="true">
                <FluentIcon name={packageStatus.icon} size={18} />
              </span>
              <StatusBadge state={packageStatus.state} label={packageStatus.stateLabel} />
            </header>
            <div>
              <h3>{t(packageStatus.label)}</h3>
              <p>{t(packageStatus.description)}</p>
            </div>
            {packageStatus.value !== packageStatus.stateLabel ? <footer>{t(packageStatus.value)}</footer> : null}
          </article>
        ))}
      </div>
    </div>
  )
}

export default function CapabilitiesPanel({ capabilities, runtimePackages, onOpenSection }: Props) {
  const [page, setPage] = useState<CapabilityPage>('overview')
  const { t } = useI18n()
  const pages: CapabilityPage[] = ['overview', 'scene_status', 'runtime_packages']
  const moveTab = (event: ReactKeyboardEvent<HTMLButtonElement>, current: CapabilityPage) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    const offset = event.key === 'ArrowRight' ? 1 : -1
    const next = pages[(pages.indexOf(current) + offset + pages.length) % pages.length]
    setPage(next)
    document.getElementById(`capability-tab-${next}`)?.focus()
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="settings-panel-title">{t('Capabilities')}</h2>
        <p className="settings-panel-description">
          {t('Read-only view of what the current runtime can provide and whether each configured implementation is ready.')}
        </p>
      </div>

      <div className="capability-page-tabs" role="tablist" aria-label="Capability views">
        <button id="capability-tab-overview" aria-controls="capability-panel-overview" type="button" role="tab" aria-selected={page === 'overview'} tabIndex={page === 'overview' ? 0 : -1} onKeyDown={event => moveTab(event, 'overview')} onClick={() => setPage('overview')}>
          {t('Overview')}
        </button>
        <button id="capability-tab-scene_status" aria-controls="capability-panel-scene_status" type="button" role="tab" aria-selected={page === 'scene_status'} tabIndex={page === 'scene_status' ? 0 : -1} onKeyDown={event => moveTab(event, 'scene_status')} onClick={() => setPage('scene_status')}>
          {t('Scene capability status')}
        </button>
        <button id="capability-tab-runtime_packages" aria-controls="capability-panel-runtime_packages" type="button" role="tab" aria-selected={page === 'runtime_packages'} tabIndex={page === 'runtime_packages' ? 0 : -1} onKeyDown={event => moveTab(event, 'runtime_packages')} onClick={() => setPage('runtime_packages')}>
          {t('Runtime packages')}
        </button>
      </div>

      <div role="tabpanel" id={`capability-panel-${page}`} aria-labelledby={`capability-tab-${page}`}>
        {page === 'overview' ? <Overview capabilities={capabilities} runtimePackages={runtimePackages} onOpenSection={onOpenSection} /> : null}
        {page === 'scene_status' ? <SceneStatus capabilities={capabilities} runtimePackages={runtimePackages} onOpenSection={onOpenSection} /> : null}
        {page === 'runtime_packages' ? <RuntimePackages runtimePackages={runtimePackages} /> : null}
      </div>
    </div>
  )
}
