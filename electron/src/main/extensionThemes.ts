import fs from 'node:fs'
import path from 'node:path'
import { nativeImage } from 'electron'
import { assertExtensionPath } from './extensionPaths.js'
import { validateThemePalette, type ThemePalette, type AppliedTheme } from '../shared/extensionTheme.js'
import type { ExtensionManifest } from '../shared/extensions.js'

export type PreparedTheme = ThemePalette & { image?: AppliedTheme['image'] }

/** Bound the declared canvas before asking the native decoder to allocate it. */
export function imageDimensions(bytes: Buffer, mime: string): { width: number; height: number } {
  if (mime === 'image/png' && bytes.length >= 24 && bytes.toString('ascii', 12, 16) === 'IHDR') {
    return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) }
  }
  if (mime === 'image/jpeg') {
    let offset = 2
    while (offset + 4 <= bytes.length && bytes[offset] === 255) {
      while (bytes[offset] === 255) offset++
      const marker = bytes[offset++]
      if (marker === 217 || marker === 218) break
      if (marker === 1 || marker >= 208 && marker <= 215) continue
      if (offset + 2 > bytes.length) break
      const length = bytes.readUInt16BE(offset)
      if (length < 2 || offset + length > bytes.length) break
      if ([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(marker) && length >= 8) {
        return { width: bytes.readUInt16BE(offset + 5), height: bytes.readUInt16BE(offset + 3) }
      }
      offset += length
    }
  }
  throw new Error('Theme background.image: invalid image header')
}

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
        const imagePath = path.join(packagePath, prepared.background.image)
        assertExtensionPath(packagePath, imagePath)
        const stat = fs.statSync(imagePath)
        if (!stat.isFile() || stat.size > 8 * 1024 * 1024) throw new Error('Theme background.image: image exceeds 8 MiB or is not a regular file')
        const bytes = fs.readFileSync(imagePath)
        const extension = path.extname(imagePath).toLowerCase()
        const mime = extension === '.png' && bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ? 'image/png'
          : ['.jpg', '.jpeg'].includes(extension) && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 ? 'image/jpeg' : ''
        if (!mime) throw new Error('Theme background.image: use a PNG or JPEG image with matching bytes')
        const declared = imageDimensions(bytes, mime)
        if (declared.width < 1 || declared.height < 1 || declared.width > 4096 || declared.height > 4096) throw new Error('Theme background.image: dimensions must be 1–4096 pixels')
        const decoded = nativeImage.createFromBuffer(bytes), size = decoded.getSize()
        if (decoded.isEmpty() || size.width < 1 || size.height < 1 || size.width > 4096 || size.height > 4096) throw new Error('Theme background.image: decoded dimensions must be 1–4096 pixels')
        if (selectedId !== undefined) prepared.image = { mime, base64: bytes.toString('base64') }
      }
      result[theme.id] = prepared
    }
    return result
  }
}
