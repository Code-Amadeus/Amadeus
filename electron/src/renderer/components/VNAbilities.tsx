import { useI18n } from '../i18n'

export type VNCapability = 'immediate' | 'interaction' | 'summary' | 'retrospective' | 'lookahead' | 'reasoning'
export type VNCapabilities = Partial<Record<VNCapability, boolean>>
export type VNCapabilityPresets = Record<'base' | 'mystery', VNCapabilities>
export type CapabilityState = { requested?: boolean; available?: boolean; enabled?: boolean; reason?: string }

export const VN_ABILITIES: Array<{ key: VNCapability; title: string; detail: string }> = [
  { key: 'immediate', title: 'Immediate commentary', detail: 'React to the current story moment.' },
  { key: 'interaction', title: 'Player interaction', detail: 'Answer your questions and respond to choices.' },
  { key: 'summary', title: 'Story summaries', detail: 'Summarize recent story events.' },
  { key: 'retrospective', title: 'Reflection', detail: 'Reflect on earlier story details.' },
  { key: 'lookahead', title: 'Lookahead', detail: 'Requires a full script and verified alignment during play.' },
  { key: 'reasoning', title: 'Detective reasoning', detail: 'Reason about clues using the Mystery companion.' },
]
const reasons: Record<string, string> = {
  disabled_by_profile: 'Not used for this game type.',
  ready: 'Available now', rules_only: 'Available through built-in story rules.',
  model_unavailable: 'The companion model is unavailable.',
  immediate_model_disabled: 'The commentary model is turned off.',
  retrospective_model_unavailable: 'The reflection model is unavailable.',
  lookahead_model_unavailable: 'The lookahead model is unavailable.',
  script_unavailable: 'Add a full story script in game settings.',
  alignment_unavailable: 'Waiting for the script to align with the game.',
  semantic_type_unsupported: 'Not used for this game type.',
  model_unsupported: 'The selected model does not support this ability.',
  model_unconfigured: 'Choose a model to use this ability.',
}
export function capabilityReason(reason?: string, enabled = false): string {
  return reason && reasons[reason] || (enabled ? 'Available now' : 'Unavailable now')
}

export default function VNAbilities({ preset, states, compact = false }: {
  preset: VNCapabilities; states?: Record<string, CapabilityState>; compact?: boolean
}) {
  const { t } = useI18n()
  return <ul className={compact ? 'vn-ability-tags' : 'vn-abilities'} aria-label={t('Companion abilities')}>
    {VN_ABILITIES.filter(ability => !compact || preset[ability.key]).map(ability => {
      const included = !!preset[ability.key]
      const state = states?.[ability.key]
      const status = !included ? 'Not included' : state ? state.enabled ? 'Available now' : 'Waiting' : 'Included'
      const detail = !included ? 'Not used for this game type.' : state ? capabilityReason(state.reason, state.enabled) : ability.detail
      return <li key={ability.key} className={!included ? 'excluded' : state?.enabled ? 'available' : 'included'}>
        <span className="vn-ability-dot" aria-hidden="true" />
        <div><strong>{t(ability.title)}</strong>{!compact && <small>{t(detail)}</small>}</div>
        {!compact && <span className="vn-ability-state">{t(status)}</span>}
      </li>
    })}
  </ul>
}
