import fs from 'node:fs/promises'
import path from 'node:path'

export type PortraitFrames = Record<string, { idle: string[]; speaking: string[] }>

/** Read the existing, optional VN cache. No character media is bundled or generated. */
export async function readCompanionPortraits(cacheDir: string): Promise<PortraitFrames> {
  if (!cacheDir) return {}
  try {
    const root = await fs.realpath(cacheDir)
    const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifest.json'), 'utf8'))
    const frames: PortraitFrames = {}
    for (const [emotion, raw] of Object.entries(manifest.emotions || {}).slice(0, 24)) {
      const entry = raw as Record<string, unknown>
      const result = { idle: [] as string[], speaking: [] as string[] }
      for (const mode of ['idle', 'speaking'] as const) {
        for (const name of (Array.isArray(entry[mode]) ? entry[mode] : []).slice(0, 12)) {
          if (typeof name !== 'string' || path.extname(name).toLowerCase() !== '.png') continue
          try {
            const file = await fs.realpath(path.resolve(root, name))
            const relative = path.relative(root, file)
            if (relative.startsWith('..') || path.isAbsolute(relative)) continue
            if ((await fs.stat(file)).size > 1024 * 1024) continue
            result[mode].push(`data:image/png;base64,${(await fs.readFile(file)).toString('base64')}`)
          } catch { /* A partial optional cache can still provide its remaining frames. */ }
        }
      }
      if (result.idle.length || result.speaking.length) frames[emotion] = result
    }
    return frames
  } catch {
    // The CPU/model-less baseline works with a text avatar.
    return {}
  }
}
