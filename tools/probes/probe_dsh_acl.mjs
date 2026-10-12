// Opt-in Windows probe. Grants only a newly created disposable test directory.
// No ACL changes to a user's existing workspace; no sandbox bypass.
import fs from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import os from 'node:os'
const root = fileURLToPath(new URL('../../', import.meta.url))
const runtime = path.join(root, 'build/deepseek-acp-live/runtime/node_modules/@deepseek-ai/dsh-sandbox-windows-acl/lib/index.js')
const { AclSandbox, workspaceWriteSid } = await import(pathToFileURL(runtime).href)
const parent = process.argv[2] === 'temp' ? os.tmpdir() : path.join(root, 'build')
const dir = await fs.mkdtemp(path.join(parent, 'amadeus-acl-probe-'))
const outside = path.join(parent, path.basename(dir) + '-outside.txt')
const sandbox = new AclSandbox({ mode: 'workspace-write', writableDirs: [dir], tempDir: null, writeSid: workspaceWriteSid(dir) })
let initialized = false
try {
  await sandbox.init()
  initialized = true
  const code = `const fs=require('node:fs'); fs.writeFileSync(${JSON.stringify(path.join(dir, 'inside.txt'))},'synthetic');
    let outsideDenied=false; try { fs.writeFileSync(${JSON.stringify(outside)},'synthetic'); } catch { outsideDenied=true; }
    console.log(JSON.stringify({insideWritten:true,outsideDenied}));`
  const child = sandbox.spawn({command: process.execPath, args: ['-e', code], cwd: dir, stdio: 'pipe'})
  const result = await child.wait()
  console.log(JSON.stringify({ location: process.argv[2] || 'checkout', initialized: true,
    exitCode: result.exitCode, result: result.stdout.toString('utf8').trim() }))
} catch (error) {
  console.log(JSON.stringify({ location: process.argv[2] || 'checkout', initialized,
    error: String(error).replaceAll(dir, '<test-workspace>') }))
} finally {
  sandbox.dispose()
  if (path.dirname(path.resolve(dir)) !== path.resolve(parent) || !path.basename(dir).startsWith('amadeus-acl-probe-')) throw new Error('Invalid cleanup target')
  await fs.rm(dir, {recursive: true, force: true})
  await fs.rm(outside, {force: true})
}
