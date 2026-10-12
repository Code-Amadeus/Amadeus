// Diagnostic only: forward stdout unchanged and record non-JSON lines.
// Never used by the production ACP adapter. Inputs must be synthetic.
import { spawn } from 'node:child_process'
import fs from 'node:fs'
const [trace, command, ...args] = process.argv.slice(2)
const child = spawn(command, args, { stdio: ['pipe', 'pipe', 'inherit'] })
process.stdin.pipe(child.stdin)
let pending = ''
child.stdout.on('data', data => {
  process.stdout.write(data)
  pending += data.toString('utf8')
  let index
  while ((index = pending.indexOf('\n')) >= 0) {
    const line = pending.slice(0, index).trim(); pending = pending.slice(index + 1)
    if (!line) continue
    try { JSON.parse(line) } catch { fs.appendFileSync(trace, line + '\n') }
  }
})
child.on('exit', code => { process.exitCode = code ?? 1; process.stdin.destroy() })
process.on('SIGTERM', () => child.kill())
