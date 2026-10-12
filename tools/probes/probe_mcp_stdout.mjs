// No Amadeus or model needed: real upstream MCP Client + tools-only server.
// Optional candidate changes diagnostic output only, in a temporary module copy.
import fs from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
const root = fileURLToPath(new URL('../../', import.meta.url))
const runtime = path.join(root, 'build/deepseek-acp-live/runtime/node_modules')
const clientFile = path.join(runtime, '@modelcontextprotocol/client/dist/index.mjs')
const archive = path.join(root, 'build/mcp-stdout-diagnosis/archive')
await fs.mkdir(archive, { recursive: true })
if (process.argv[2] === 'child') {
  const { Client } = await import(pathToFileURL(process.argv[3]).href)
  const { StdioClientTransport } = await import(pathToFileURL(path.join(runtime, '@modelcontextprotocol/client/dist/stdio.mjs')).href)
  const client = new Client({ name: 'synthetic-client', version: '1' })
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [path.join(runtime, '@modelcontextprotocol/server-filesystem/dist/index.js'), archive], stderr: 'ignore' })
  try {
    await client.connect(transport)
    console.log(JSON.stringify({ resources: await client.listResources(), templates: await client.listResourceTemplates() }))
  } finally { await client.close() }
} else {
  const run = file => {
    const raw = execFileSync(process.execPath, [fileURLToPath(import.meta.url), 'child', file], { encoding: 'utf8', timeout: 15000, stdio: ['ignore', 'pipe', 'pipe'] })
    const lines = raw.trim().split('\n'), nonProtocol = [], messages = []
    for (const line of lines) { try { messages.push(JSON.parse(line)) } catch { nonProtocol.push(line) } }
    return { nonProtocol, messages }
  }
  const original = await fs.readFile(clientFile, 'utf8')
  const candidate = path.join(path.dirname(clientFile), `diagnostic-${randomUUID()}.mjs`)
  try {
    const replacement = original.replaceAll('console.debug(', 'console.error(')
    if (replacement === original) throw new Error('Expected SDK diagnostics are absent; inspect the new version')
    await fs.writeFile(candidate, replacement, { flag: 'wx' })
    const report = { upstream: run(clientFile), stderrCandidate: run(candidate) }
    report.resultsUnchanged = JSON.stringify(report.upstream.messages) === JSON.stringify(report.stderrCandidate.messages)
    await fs.writeFile(path.join(root, 'build/mcp-stdout-diagnosis/report.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report))
  } finally { await fs.unlink(candidate) }
}
