import { readExtensionImage } from './extensionImages.js'
import { validateThemePalette, type ThemePalette, type AppliedTheme } from '../shared/extensionTheme.js'
import type { ExtensionManifest } from '../shared/extensions.js'

export type PreparedTheme = ThemePalette & { image?: AppliedTheme['image'] }

export class ExtensionThemeResources {
  constructor(private readonly installedFonts: () => Promise<string[]> = async () => []) {}

  async validate(manifest: ExtensionManifest, packagePath?: string, selectedId?: string): Promise<Record<string, PreparedTheme>> {
    const themes = (manifest.contributes.theme || []).filter(theme => selectedId === undefined || theme.id === selectedId)
    const needsFonts = themes.some(theme => Object.keys(theme.tokens || {}).some(key => key === '--font' || key === '--font-cjk'))
    const fonts = needsFonts ? await this.installedFonts() : []
    const result: Record<string, PreparedTheme> = {}
    for (const theme of themes) {
      const prepared: PreparedTheme = validateThemePalette(theme, fonts)
      if (packagePath && prepared.background) {
        const image = readExtensionImage(packagePath, prepared.background.image,
          { label: 'Theme background.image', maxMiB: 8, maxDimension: 4096 })
        if (selectedId !== undefined) prepared.image = image
      }
      result[theme.id] = prepared
    }
    return result
  }
}
