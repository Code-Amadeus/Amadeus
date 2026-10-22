// Explicit developer/CI shell; never label this as a full application distribution.
const config = require('./package.json').build
module.exports = {
  ...config,
  artifactName: 'amadeus-code-only-${version}-${arch}.${ext}',
  beforePack: 'scripts/build-windows-wallpaper.cjs',
  extraResources: config.extraResources.filter(item => item.to !== 'assets/bundled/japanese-pronunciation'),
}
