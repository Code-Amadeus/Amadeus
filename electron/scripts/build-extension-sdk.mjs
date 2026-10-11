import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { projectPythonCommand } from '../src/main/pythonRuntime.ts'

const root = fileURLToPath(new URL('../..', import.meta.url))
const result = spawnSync(projectPythonCommand(root), ['-I', path.join(root, 'tools/build_extension_sdk.py')],
  { cwd: root, stdio: 'inherit', windowsHide: true })
if (result.error) console.error(`Cannot build the bundled extension SDK: ${result.error.message}. Set AMADEUS_PYTHON to the documented development environment.`)
process.exitCode = result.status ?? 1
