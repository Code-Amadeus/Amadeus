import { nativeImage } from 'electron'
import fs from 'fs'
import path from 'path'

export type ChatAvatarRole = 'user' | 'assistant'

const AVATAR_SIZE = 128
const MAX_SOURCE_BYTES = 20 * 1024 * 1024

export class ChatAvatarStore {
  constructor(private readonly directory: string) {}

  private avatarPath(role: ChatAvatarRole): string {
    return path.join(this.directory, `${role}.png`)
  }

  private normalizedRole(value: unknown): ChatAvatarRole {
    if (value === 'user' || value === 'assistant') return value
    throw new Error('Unsupported chat avatar role')
  }

  snapshot(): { user: string; assistant: string } {
    return {
      user: this.dataUrl('user'),
      assistant: this.dataUrl('assistant'),
    }
  }

  save(roleValue: unknown, sourcePathValue: unknown): { user: string; assistant: string } {
    const role = this.normalizedRole(roleValue)
    const sourcePath = String(sourcePathValue || '')
    const stat = fs.statSync(sourcePath)
    if (!stat.isFile() || stat.size <= 0 || stat.size > MAX_SOURCE_BYTES) {
      throw new Error('Avatar image must be a file smaller than 20 MiB')
    }
    const image = nativeImage.createFromPath(sourcePath)
    if (image.isEmpty()) throw new Error('Could not decode the selected avatar image')
    const size = image.getSize()
    if (size.width < 16 || size.height < 16) throw new Error('Avatar image is too small')
    const side = Math.min(size.width, size.height)
    const square = image.crop({
      x: Math.floor((size.width - side) / 2),
      y: Math.floor((size.height - side) / 2),
      width: side,
      height: side,
    }).resize({ width: AVATAR_SIZE, height: AVATAR_SIZE, quality: 'best' })
    fs.mkdirSync(this.directory, { recursive: true })
    const target = this.avatarPath(role)
    const temporary = `${target}.tmp`
    fs.writeFileSync(temporary, square.toPNG(), { mode: 0o600 })
    fs.renameSync(temporary, target)
    return this.snapshot()
  }

  clear(roleValue: unknown): { user: string; assistant: string } {
    const role = this.normalizedRole(roleValue)
    const target = this.avatarPath(role)
    try {
      fs.rmSync(target)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    return this.snapshot()
  }

  private dataUrl(role: ChatAvatarRole): string {
    try {
      const image = nativeImage.createFromPath(this.avatarPath(role))
      return image.isEmpty() ? '' : image.toDataURL()
    } catch {
      return ''
    }
  }
}
