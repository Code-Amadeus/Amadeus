import type { ModelConnectionCatalogField, ModelConnectionCatalogGroup } from './modelConnectionCatalog'

interface RoleDesktopSnapshot {
  values?: Record<string, string>
}

const field = (
  key: string,
  label: string,
  type: ModelConnectionCatalogField['type'],
  value: string | boolean = '',
  options?: ModelConnectionCatalogField['options'],
  description?: string,
): ModelConnectionCatalogField => ({
  key,
  label,
  type,
  value,
  options,
  description,
  editable: true,
  restart_required: true,
})

export function buildModelRoleCatalog(
  snapshot?: RoleDesktopSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const value = (key: string, fallback = '') => values[key] ?? fallback
  const providerOptions = [
    { value: '', label: 'Inherit' },
    { value: 'deepseek', label: 'DeepSeek' },
    { value: 'openai', label: 'OpenAI-compatible' },
  ]
  const translationProviderOptions = [
    { value: '', label: 'Recommended default · DeepSeek' },
    { value: 'deepseek', label: 'DeepSeek' },
    { value: 'openai', label: 'OpenAI-compatible' },
  ]
  const role = (
    id: string,
    label: string,
    description: string,
    fields: ModelConnectionCatalogField[],
    active = false,
  ): ModelConnectionCatalogGroup => ({
    id,
    label,
    description,
    active,
    configured: true,
    status: 'Backend status unavailable',
    status_ok: false,
    fields,
  })

  return [
    role('vn_companion', 'VN companion', 'Dedicated VN reasoning and reaction role. DeepSeek is the recommended default; OpenAI-compatible is also supported.', [
      field('VN_LLM_PROVIDER', 'Model connection', 'select', value('VN_LLM_PROVIDER', 'deepseek'), [
        { value: 'deepseek', label: 'DeepSeek · Recommended' },
        { value: 'openai', label: 'OpenAI-compatible' },
      ]),
      field('VN_LLM_MODEL', 'Model override', 'text', value('VN_LLM_MODEL'), undefined, 'Optional. Leave blank to use the model from the selected connection.'),
    ], true),
    role('work_planner', 'Work planner / router', 'Plans and routes cooperative Work; an empty model inherits the main conversation model.', [
      field('COOPERATIVE_WORK_PLANNER_MODEL', 'Model override', 'text', value('COOPERATIVE_WORK_PLANNER_MODEL'), undefined, 'Leave blank to inherit the main conversation model.'),
    ]),
    role('work_observer', 'Work observer', 'Summarizes Provider progress; empty overrides inherit the main conversation model.', [
      field('WORK_OBSERVER_PROVIDER', 'Provider override', 'select', value('WORK_OBSERVER_PROVIDER'), providerOptions),
      field('WORK_OBSERVER_MODEL', 'Model override', 'text', value('WORK_OBSERVER_MODEL'), undefined, 'Leave blank to use the selected provider connection model.'),
    ]),
    role('browser_branch_planner', 'Browser branch planner', 'Chooses bounded browser branches; empty overrides inherit a supported main provider.', [
      field('BROWSER_BRANCH_PROVIDER', 'Provider override', 'select', value('BROWSER_BRANCH_PROVIDER'), providerOptions),
      field('BROWSER_BRANCH_MODEL', 'Model override', 'text', value('BROWSER_BRANCH_MODEL'), undefined, 'Leave blank to use the selected provider connection model.'),
    ]),
    role('auip_action', 'AUIP action decision', 'Decision-quality model used by the B2 application action path.', [
      field('AUIP_ACTION_PROVIDER', 'Provider override', 'select', value('AUIP_ACTION_PROVIDER'), providerOptions),
      field('AUIP_ACTION_MODEL', 'Model override', 'text', value('AUIP_ACTION_MODEL')),
      field('AUIP_ACTION_REASONING_EFFORT', 'Reasoning effort', 'select', value('AUIP_ACTION_REASONING_EFFORT', 'high'), ['none', 'minimal', 'low', 'medium', 'high', 'max']),
      field('AUIP_ACTION_SERVICE_TIER', 'Service tier', 'select', value('AUIP_ACTION_SERVICE_TIER', 'auto'), ['auto', 'default', 'fast', 'priority']),
    ]),
    role('auip_narration', 'AUIP narration', 'Narrates verified application outcomes; empty overrides inherit Work observer and then Main conversation.', [
      field('AUIP_NARRATION_PROVIDER', 'Provider override', 'select', value('AUIP_NARRATION_PROVIDER'), providerOptions),
      field('AUIP_NARRATION_MODEL', 'Model override', 'text', value('AUIP_NARRATION_MODEL')),
    ]),
    role('vn_subtitle_translation', 'VN subtitle translation', 'Translates Japanese game dialogue into Simplified Chinese for display.', [
      field('VN_SUBTITLE_TRANSLATE_PROVIDER', 'Model connection', 'select', value('VN_SUBTITLE_TRANSLATE_PROVIDER'), translationProviderOptions),
      field('VN_SUBTITLE_TRANSLATE_MODEL', 'Model override', 'text', value('VN_SUBTITLE_TRANSLATE_MODEL'), undefined, 'Leave blank to use the model from the selected connection.'),
    ]),
    role('vn_speech_translation', 'VN speech translation', 'Translates Chinese companion reactions into Japanese before speech synthesis.', [
      field('VN_TTS_TRANSLATE_PROVIDER', 'Model connection', 'select', value('VN_TTS_TRANSLATE_PROVIDER'), translationProviderOptions),
      field('VN_TTS_TRANSLATE_MODEL', 'Model override', 'text', value('VN_TTS_TRANSLATE_MODEL'), undefined, 'Leave blank to use the model from the selected connection.'),
    ]),
  ]
}
