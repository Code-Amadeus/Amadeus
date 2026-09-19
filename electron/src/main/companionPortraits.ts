import fs from 'node:fs/promises'
import path from 'node:path'

export type PortraitFrames = Record<string, { idle: string[]; speaking: string[] }>

export type CompanionPortraitStatus = {
  installed: boolean
  state: 'ready' | 'not_installed' | 'incomplete' | 'invalid'
  emotionCount: number
  frameCount: number
  detail: string
}

type PortraitManifest = { root: string; emotions: Record<string, unknown> }

async function readPortraitManifest(cacheDir: string): Promise<PortraitManifest> {
  const root = await fs.realpath(cacheDir)
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifest.json'), 'utf8')) as Record<string, unknown>
  if (!manifest.emotions || typeof manifest.emotions !== 'object' || Array.isArray(manifest.emotions)) {
    throw new Error('manifest.json has no emotions map')
  }
  return { root, emotions: manifest.emotions as Record<string, unknown> }
}

async function validatedPortraitPath(root: string, name: unknown): Promise<string | null> {
  if (typeof name !== 'string' || path.extname(name).toLowerCase() !== '.png') return null
  try {
    const file = await fs.realpath(path.resolve(root, name))
    const relative = path.relative(root, file)
    if (relative.startsWith('..') || path.isAbsolute(relative)) return null
    if ((await fs.stat(file)).size > 1024 * 1024) return null
    return file
  } catch {
    return null
  }
}

/** Read the existing, optional VN cache. No character media is bundled or generated. */
export async function readCompanionPortraits(cacheDir: string): Promise<PortraitFrames> {
  if (!cacheDir) return {}
  try {
    const { root, emotions } = await readPortraitManifest(cacheDir)
    const frames: PortraitFrames = {}
    for (const [emotion, raw] of Object.entries(emotions).slice(0, 24)) {
      const entry = raw as Record<string, unknown>
      const result = { idle: [] as string[], speaking: [] as string[] }
      for (const mode of ['idle', 'speaking'] as const) {
        for (const name of (Array.isArray(entry[mode]) ? entry[mode] : []).slice(0, 12)) {
          const file = await validatedPortraitPath(root, name)
          if (file) result[mode].push(`data:image/png;base64,${(await fs.readFile(file)).toString('base64')}`)
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

/** Report the baked VN portrait asset boundary without loading image bytes. */
export async function companionPortraitStatus(cacheDir: string): Promise<CompanionPortraitStatus> {
  try {
    const { root, emotions } = await readPortraitManifest(cacheDir)
    const validFiles = new Set<string>()
    let emotionCount = 0
    let invalidFrames = 0
    for (const [, raw] of Object.entries(emotions).slice(0, 24)) {
      const entry = raw as Record<string, unknown>
      let emotionHasFrame = false
      for (const mode of ['idle', 'speaking'] as const) {
        for (const name of (Array.isArray(entry[mode]) ? entry[mode] : []).slice(0, 12)) {
          const file = await validatedPortraitPath(root, name)
          if (!file) {
            invalidFrames += 1
            continue
          }
          validFiles.add(file)
          emotionHasFrame = true
        }
      }
      if (emotionHasFrame) emotionCount += 1
    }
    if (!validFiles.size) {
      return {
        installed: false,
        state: 'incomplete',
        emotionCount: 0,
        frameCount: 0,
        detail: 'The baked VN portrait manifest contains no loadable PNG frames.',
      }
    }
    const incomplete = invalidFrames > 0
    return {
      installed: !incomplete,
      state: incomplete ? 'incomplete' : 'ready',
      emotionCount,
      frameCount: validFiles.size,
      detail: incomplete
        ? `${emotionCount} emotions are usable, but some manifest frames are missing or invalid.`
        : `${emotionCount} emotions and ${validFiles.size} unique baked PNG frames are available.`,
    }
  } catch (error) {
    const code = (error as NodeJS.ErrnoException)?.code
    if (code === 'ENOENT') {
      return {
        installed: false,
        state: 'not_installed',
        emotionCount: 0,
        frameCount: 0,
        detail: 'Optional baked VN companion portraits are not installed.',
      }
    }
    return {
      installed: false,
      state: 'invalid',
      emotionCount: 0,
      frameCount: 0,
      detail: `The VN portrait manifest could not be read: ${error instanceof Error ? error.message : String(error)}`,
    }
  }
}
