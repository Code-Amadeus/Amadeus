import assert from 'node:assert/strict'
import fs from 'node:fs'

const source = new URL('../../server/extensions/profile.json', import.meta.url)
const target = new URL('../src/shared/extensionProfile.generated.ts', import.meta.url)
const profile = JSON.parse(fs.readFileSync(source, 'utf8'))
const ids = new Set()
for (const row of profile) {
  const key = `${row.kind}:${row.role || ''}`
  assert.ok(!ids.has(key), `Duplicate contribution profile ${key}`)
  ids.add(key)
  assert.ok(['open', 'closed'].includes(row.status))
  assert.ok(['data', 'web', 'process'].includes(row.tier))
  assert.ok(['frontend', 'backend_restart'].includes(row.apply))
  assert.ok(['select', 'bind', 'present'].includes(row.attach))
  assert.ok(row.status === 'open' || row.reason)
  assert.ok(Array.isArray(row.bind_targets) && Array.isArray(row.select_keys))
}
const generated = `// Generated from server/extensions/profile.json; do not edit.\nexport const extensionProfile = ${JSON.stringify(profile, null, 2)} as const\n`
if (process.argv.includes('--check')) {
  assert.equal(fs.readFileSync(target, 'utf8'), generated, 'Extension profile is out of date')
} else fs.writeFileSync(target, generated)
console.log(`Extension profile: ${profile.length} contribution types`)
