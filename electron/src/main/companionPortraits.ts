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

type PortraitManifest = { root: string; format?: unknown; emotions: Record<string, unknown> }
type CompanionAtlasSpec = { url?: unknown; sequence?: unknown; fileBytes?: unknown }

const COMPANION_ATLAS_FORMAT = 'amadeus.companion-atlas.v1'
const COMPANION_ATLAS_URL = /^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.webp$/
const COMPANION_ATLAS_BYTE_LIMIT = 16 * 1024 * 1024

async function readPortraitManifest(cacheDir: string): Promise<PortraitManifest> {
  const root = await fs.realpath(cacheDir)
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifest.json'), 'utf8')) as Record<string, unknown>
  if (!manifest.emotions || typeof manifest.emotions !== 'object' || Array.isArray(manifest.emotions)) {
    throw new Error('manifest.json has no emotions map')
  }
  return { root, format: manifest.format, emotions: manifest.emotions as Record<string, unknown> }
}

async function validatedCompanionAtlasSpec(root: string, raw: unknown): Promise<{ file: string; frameCount: number } | null> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const spec = raw as CompanionAtlasSpec
  if (typeof spec.url !== 'string' || !COMPANION_ATLAS_URL.test(spec.url)) return null
  if (!Array.isArray(spec.sequence) || !spec.sequence.length) return null
  if (typeof spec.fileBytes !== 'number' || !Number.isInteger(spec.fileBytes) || spec.fileBytes <= 0) return null
  try {
    const file = await fs.realpath(path.resolve(root, spec.url))
    const relative = path.relative(root, file)
    if (relative.startsWith('..') || path.isAbsolute(relative)) return null
    const size = (await fs.stat(file)).size
    if (size !== spec.fileBytes || size > COMPANION_ATLAS_BYTE_LIMIT) return null
    return { file, frameCount: spec.sequence.length }
  } catch {
    return null
  }
}

async function companionAtlasStatus(root: string, emotions: Record<string, unknown>): Promise<CompanionPortraitStatus> {
  const validFiles = new Set<string>()
  let emotionCount = 0
  let frameCount = 0
  let invalidFrames = 0
  for (const raw of Object.values(emotions).slice(0, 24)) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      invalidFrames += 1
      continue
    }
    const states = raw as Record<string, unknown>
    let emotionHasFrame = false
    for (const mode of ['idle', 'speaking', 'idleStatic', 'speakingAlternate']) {
      if (!(mode in states)) continue
      const result = await validatedCompanionAtlasSpec(root, states[mode])
      if (!result) {
        invalidFrames += 1
        continue
      }
      validFiles.add(result.file)
      frameCount += result.frameCount
      emotionHasFrame = true
    }
    if (emotionHasFrame) emotionCount += 1
  }
  if (!validFiles.size) {
    return {
      installed: false,
      state: 'incomplete',
      emotionCount: 0,
      frameCount: 0,
      detail: 'The baked VN portrait manifest contains no loadable WebP atlas frames.',
    }
  }
  const incomplete = invalidFrames > 0
  return {
    installed: !incomplete,
    state: incomplete ? 'incomplete' : 'ready',
    emotionCount,
    frameCount,
    detail: incomplete
      ? `${emotionCount} emotions are usable, but some manifest atlas files are missing or invalid.`
      : `${emotionCount} emotions and ${frameCount} baked atlas frames are available.`,
  }
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
    const { root, format, emotions } = await readPortraitManifest(cacheDir)
    if (format === COMPANION_ATLAS_FORMAT) {
      return companionAtlasStatus(root, emotions)
    }
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
