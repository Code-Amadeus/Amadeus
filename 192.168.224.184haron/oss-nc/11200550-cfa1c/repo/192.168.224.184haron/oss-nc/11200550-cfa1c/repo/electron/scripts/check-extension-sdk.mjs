import { fileURLToPath } from 'node:url'
import { bundledExtensionSdk } from '../src/main/extensionSdk.ts'

bundledExtensionSdk(fileURLToPath(new URL('../../', import.meta.url)))
console.log('Bundled extension SDK SHA-256 verified')
