const fs = require('node:fs')
const path = require('node:path')
const { createHash } = require('node:crypto')
const buildWallpaper = require('./build-windows-wallpaper.cjs')

function verifyDictionary(directory, pin) {
  const names = ['userdict.csv', 'provenance.json', 'NOTICE.md', 'GPT-SoVITS-MIT.txt',
    'CMUdict-BSD-2-Clause.txt', 'Amadeus-AGPL-3.0.txt', 'source.csv', 'overlay.csv', 'OpenJTalk-COPYING.txt'].sort()
  const expected = pin.files.map(record => path.basename(record.path)).sort()
  if (pin.format !== 'amadeus.bundled-japanese-dictionary.v1' || pin.pack_id !== 'japanese-pronunciation'
      || JSON.stringify(expected) !== JSON.stringify(names)
      || pin.files.some(record => record.path !== `assets/text/ja/english/${path.basename(record.path)}`)) {
    throw new Error('Invalid bundled dictionary pin; personal/compiled files are not distributable')
  }
  if (!fs.existsSync(directory) || JSON.stringify(fs.readdirSync(directory).sort()) !== JSON.stringify(expected)) {
    throw new Error('Pinned Japanese dictionary is missing. Run tools/stage_japanese_dictionary.py --archive <pinned.zip> before application packaging.')
  }
  for (const record of pin.files) {
    const data = fs.readFileSync(path.join(directory, path.basename(record.path)))
    if (data.length !== record.size || createHash('sha256').update(data).digest('hex') !== record.sha256) {
      throw new Error(`Bundled Japanese dictionary differs from release pin: ${path.basename(record.path)}`)
    }
  }
}

function prepareDictionary(root) {
  const pin = JSON.parse(fs.readFileSync(path.join(root, 'release/japanese_dictionary.json'), 'utf8'))
  const staged = path.join(root, 'build/bundled-assets/assets/bundled/japanese-pronunciation')
  const inSource = path.join(root, 'assets/bundled/japanese-pronunciation')
  const source = fs.existsSync(inSource) ? inSource : staged
  verifyDictionary(source, pin)
  if (source !== staged) {
    fs.mkdirSync(staged, { recursive: true })
    for (const record of pin.files) fs.copyFileSync(path.join(source, path.basename(record.path)), path.join(staged, path.basename(record.path)))
    verifyDictionary(staged, pin)
  }
  return staged
}

module.exports = async context => {
  prepareDictionary(path.resolve(__dirname, '../..'))
  await buildWallpaper(context)
}
module.exports.prepareDictionary = prepareDictionary
module.exports.verifyDictionary = verifyDictionary
