import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

/** Use the same pinned artifact in source and packaged installations. */
export function bundledExtensionSdk(projectRoot: string, directory = path.join(projectRoot, 'vendor', 'extension-sdk')): string {
  const { sdk } = JSON.parse(fs.readFileSync(path.join(projectRoot, 'server', 'extensions', 'protocol-source.json'), 'utf8'))
  if (!sdk || typeof sdk.filename !== 'string' || path.basename(sdk.filename) !== sdk.filename
    || !/^[A-Za-z0-9_.-]+\.whl$/.test(sdk.filename) || typeof sdk.version !== 'string'
    || !/^[a-f0-9]{64}$/.test(sdk.sha256)) throw new Error('The bundled extension SDK manifest is invalid')
  const wheel = path.join(directory, sdk.filename)
  if (!fs.existsSync(wheel)) throw new Error('The bundled extension SDK wheel is unavailable')
  if (createHash('sha256').update(fs.readFileSync(wheel)).digest('hex') !== sdk.sha256) {
    throw new Error('The bundled extension SDK failed SHA-256 verification')
  }
  return wheel
}
