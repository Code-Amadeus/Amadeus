// Exercise real Electron cleanup with an intentional assertion in each smoke.
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const electronRoot = path.resolve(__dirname, '..')
const output = path.resolve(electronRoot, '../output/diagnostics/smoke-exit-codes')
const scripts = ['extensionSettings', 'frameSourceSettings', 'workArtifacts']
const results = []
fs.mkdirSync(output, { recursive: true })
for (const name of scripts) {
  const original = path.join(electronRoot, 'tests', `${name}.smoke.mjs`)
  const probe = path.join(electronRoot, 'tests', `.smoke-exit-${name}-${process.pid}.mjs`)
  const marker = `intentional smoke assertion: ${name}`
  const source = fs.readFileSync(original, 'utf8')
  const injected = source.replace(
    /(^[ \t]*await window\.loadURL\([^\r\n]+\)[;]?)(\r?\n)/m,
    `$1$2  console.log(${JSON.stringify(marker)})\n  assert.fail(${JSON.stringify(marker)})\n`,
  ).replace('output/diagnostics/', 'output/diagnostics/smoke-exit-codes/')
  assert.notEqual(injected, source, `Could not inject failure into ${name}`)
  assert.ok(injected.includes(`assert.fail(${JSON.stringify(marker)})`))
  fs.writeFileSync(probe, injected, { flag: 'wx' })
  let exitCode = 0
  let text = ''
  try {
    try {
      text = execFileSync(require('electron'), [probe], {
        cwd: electronRoot, encoding: 'utf8', windowsHide: true, timeout: 120000,
      })
    } catch (error) {
      exitCode = error.status
      text = String(error.stdout || '') + String(error.stderr || '')
    }
  } finally {
    fs.unlinkSync(probe)
  }
  results.push({ script: `${name}.smoke.mjs`, exitCode, failureReached: text.includes(marker) })
}
const report = { passed: results.every(row => row.exitCode === 1 && row.failureReached), results }
fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n')
console.log(JSON.stringify(report, null, 2))
assert.ok(report.passed, 'Intentional smoke assertions must propagate exit code 1 to execFileSync')
