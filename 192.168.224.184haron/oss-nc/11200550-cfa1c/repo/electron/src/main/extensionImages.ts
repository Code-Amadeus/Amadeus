import fs from 'node:fs'
import path from 'node:path'
import { nativeImage } from 'electron'
import { assertExtensionPath } from './extensionPaths.js'

/** Bound the declared canvas before asking the native decoder to allocate it. */
export function imageDimensions(bytes: Buffer, mime: string, label = 'Extension image'): { width: number; height: number } {
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
  throw new Error(`${label}: invalid image header`)
}

/** Read only a bounded, decoded package image; renderer callers never supply a filesystem path. */
export function readExtensionImage(packagePath: string, relative: string,
  limits: { label: string; maxMiB: number; maxDimension: number }): { mime: string; base64: string } {
  const { label, maxMiB, maxDimension } = limits
  if (!/^[A-Za-z0-9_][A-Za-z0-9._-]*(\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/.test(relative)) throw new Error(`${label}: invalid package-relative path`)
  const imagePath = path.join(packagePath, relative)
  assertExtensionPath(packagePath, imagePath)
  const stat = fs.statSync(imagePath)
  if (!stat.isFile() || stat.size > maxMiB * 1024 * 1024) throw new Error(`${label}: image exceeds ${maxMiB} MiB or is not a regular file`)
  const bytes = fs.readFileSync(imagePath)
  const extension = path.extname(imagePath).toLowerCase()
  const mime = extension === '.png' && bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ? 'image/png'
    : ['.jpg', '.jpeg'].includes(extension) && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 ? 'image/jpeg' : ''
  if (!mime) throw new Error(`${label}: use a PNG or JPEG image with matching bytes`)
  const declared = imageDimensions(bytes, mime, label)
  if (declared.width < 1 || declared.height < 1 || declared.width > maxDimension || declared.height > maxDimension) throw new Error(`${label}: dimensions must be 1–${maxDimension} pixels`)
  const decoded = nativeImage.createFromBuffer(bytes), size = decoded.getSize()
  if (decoded.isEmpty() || size.width < 1 || size.height < 1 || size.width > maxDimension || size.height > maxDimension) throw new Error(`${label}: decoded dimensions must be 1–${maxDimension} pixels`)
  return { mime, base64: bytes.toString('base64') }
}
