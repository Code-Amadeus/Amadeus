import path from 'node:path'

/** The executable chooses its immutable default; Python keeps explicit installs first. */
export function bundledDictionaryEnvironment(projectRoot: string, resourcesPath: string, packaged: boolean): Record<string, string> {
  return { AMADEUS_BUNDLED_JAPANESE_DICTIONARY: path.join(packaged ? resourcesPath : projectRoot,
    'assets', 'bundled', 'japanese-pronunciation') }
}
