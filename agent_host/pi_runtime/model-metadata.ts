/** 官方旧别名只修补当前选择，不替换原生 Provider 或配置。 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { parse, type ParseError } from 'jsonc-parser'
import { getAgentDir, type ExtensionAPI, type ExtensionContext } from '@earendil-works/pi-coding-agent'

const legacyFlashIds = ['deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'] as const

const declarationError = () => new Error('Amadeus model metadata: native models.json declarations could not be read')

function readAliasDeclarations(): ReadonlySet<string> {
  let content: string
  try {
    content = readFileSync(path.join(getAgentDir(), 'models.json'), 'utf8')
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return new Set()
    throw declarationError()
  }
  const errors: ParseError[] = []
  const config = parse(content.replace(/^\uFEFF/, ''), errors, { allowTrailingComma: true, disallowComments: false })
  if (errors.length || !config || typeof config !== 'object') throw declarationError()
  const provider = config.providers?.deepseek
  return new Set(legacyFlashIds.filter(id =>
    provider?.models?.some((model: { id?: unknown }) => model?.id === id) ||
    Object.hasOwn(provider?.modelOverrides ?? {}, id)))
}

export function registerHostModelMetadata(pi: ExtensionAPI): void {
  let declarations: ReadonlySet<string> | undefined
  let skipped = false
  function skipRepair(ctx: ExtensionContext) {
    if (skipped) return
    skipped = true
    ctx.ui.notify('Amadeus: legacy DeepSeek image compatibility was skipped; keeping the Pi model selection.', 'warning')
  }
  async function repairSelection(_event: unknown, ctx: ExtensionContext) {
    const current = ctx.model
    if (skipped || current?.provider !== 'deepseek' || !legacyFlashIds.some(id => id === current.id)) return
    try {
      const registry = ctx.modelRegistry
      if (registry.find(current.provider, current.id) || registry.getRegisteredNativeProvider(current.provider) ||
          registry.getRegisteredProviderConfig(current.provider) || !registry.hasConfiguredAuth(current)) return
      // 原生配置已经加载；首次符合条件时只保存声明键，后续轮次不重读或重载配置。
      declarations ??= readAliasDeclarations()
      if (declarations.has(current.id)) return
      // 全局错误也可能来自无关 Provider；只跳过可选修补，不升级为扩展错误。
      const canonical = registry.find('deepseek', 'deepseek-flash')
      if (registry.getError() || !canonical) return skipRepair(ctx)
      const corrected = { ...structuredClone(canonical), id: current.id }
      if (JSON.stringify(current) !== JSON.stringify(corrected) && !await pi.setModel(corrected)) skipRepair(ctx)
    } catch {
      skipRepair(ctx)
    }
  }
  pi.on('session_start', repairSelection)
  pi.on('before_agent_start', repairSelection)
}
