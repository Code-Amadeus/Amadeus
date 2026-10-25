import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { createRequire } from 'node:module'
import { spawn, execFileSync } from 'node:child_process'
import { once } from 'node:events'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const require = createRequire(import.meta.url)
const { projectPythonCommand } = loadTypeScript(new URL('../src/main/pythonRuntime.ts', import.meta.url))
const sourceHelper = path.resolve(import.meta.dirname, '../resources/windows-wallpaper/helper.py')
const processCheck = `import ctypes, importlib.util, sys
from ctypes import wintypes as w
spec = importlib.util.spec_from_file_location('check_helper', sys.argv[1])
h = importlib.util.module_from_spec(spec); sys.modules[spec.name] = h; spec.loader.exec_module(h)
identity = h.ProcessIdentity(int(sys.argv[2]), int(sys.argv[3]))
windows = h.Windows()
if len(sys.argv) == 4:
    print(int(windows.is_running(identity)))
else:
    handle = windows.api.OpenProcess(0x100001 | 0x1000, False, identity.Id)
    if handle:
        try:
            created, exited, kernel, user = (w.FILETIME() for _ in range(4))
            windows.check(windows.api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)))
            actual = ((created.dwHighDateTime << 32) | created.dwLowDateTime) + h.DOTNET_FILETIME_OFFSET
            if actual == identity.Started and windows.api.WaitForSingleObject(handle, 0) != 0:
                windows.api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
                windows.api.TerminateProcess.restype = w.BOOL
                terminated = windows.api.TerminateProcess(handle, 1)
                if not terminated and windows.api.WaitForSingleObject(handle, 0) != 0:
                    windows.check(terminated)
                windows.api.WaitForSingleObject(handle, 10000)
        finally:
            windows.close(handle)
`

async function waitFor(check, description, timeout = 20000) {
  const until = Date.now() + timeout
  while (Date.now() < until) {
    if (await check()) return
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  throw new Error(`Timed out: ${description}`)
}
async function exists(file) {
  try { await fs.access(file); return true } catch (error) { if (error.code === 'ENOENT') return false; throw error }
}

for (const mode of ['attached-control', 'crash', 'normal']) {
  test(`real Electron parent lifetime preserves helper recovery (${mode})`, {
    skip: process.platform !== 'win32', timeout: 60000,
  }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'amadeus-wallpaper-parent-'))
    let parent, identity, python
    try {
      const basePython = projectPythonCommand(path.resolve(import.meta.dirname, '../..'))
      execFileSync(basePython, ['-I', '-S', '-m', 'venv', '--without-pip', path.join(root, 'runtime')], { windowsHide: true })
      python = path.join(root, 'runtime/Scripts/python.exe')
      const helperDir = path.join(root, 'electron/resources/windows-wallpaper')
      await fs.mkdir(helperDir, { recursive: true })
      await fs.copyFile(new URL('./fixtures/wallpaper_helper_fixture.py', import.meta.url), path.join(helperDir, 'helper.py'))
      if (mode === 'normal') await fs.writeFile(path.join(root, 'cleanup-allow'), '')
      const env = { ...process.env, AMADEUS_WALLPAPER_TEST_STATE: root, AMADEUS_WALLPAPER_TEST_HELPER: sourceHelper }
      delete env.ELECTRON_RUN_AS_NODE
      parent = spawn(require('electron'), [path.resolve(import.meta.dirname, 'fixtures/wallpaper-parent.cjs'), root, python, mode], {
        env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      })
      let output = '', errors = ''
      parent.stdout.on('data', data => { output += data })
      parent.stderr.on('data', data => { errors += data })
      const exited = once(parent, 'exit')
      await waitFor(() => output.includes('"ready":true') || parent.exitCode !== null, 'Electron helper ready')
      assert(output.includes('"ready":true'), output + errors)
      identity = JSON.parse(await fs.readFile(path.join(root, 'started.json'), 'utf8'))
      const pids = JSON.parse(await fs.readFile(path.join(root, 'parent.json'), 'utf8'))
      assert.equal(pids.parent, parent.pid)
      assert.notEqual(pids.launcher, identity.Id, 'exercise the Windows venv launcher and its interpreter child')
      const alive = () => execFileSync(python, ['-I', '-S', '-c', processCheck, sourceHelper, String(identity.Id), identity.Started], {
        encoding: 'utf8', windowsHide: true, timeout: 10000,
      }).trim() === '1'
      if (mode !== 'normal') {
        execFileSync('taskkill.exe', ['/PID', String(parent.pid), '/F'], { windowsHide: true })
        await exited
        await fs.writeFile(path.join(root, 'cleanup-allow'), '')
      } else {
        assert.equal((await exited)[0], 0, errors)
      }
      if (mode === 'attached-control') {
        await waitFor(() => !alive(), 'attached helper killed by Electron Job')
        assert.equal(await exists(path.join(root, 'restored.json')), false)
        assert.equal(await exists(path.join(root, 'local/Amadeus/wallpaper/session.json')), true)
      } else {
        await waitFor(() => exists(path.join(root, 'restored.json')), 'real Session restore after Electron exit')
        assert.deepEqual(JSON.parse(await fs.readFile(path.join(root, 'restored.json'), 'utf8')), { restored: true, journalRemoved: true })
        await waitFor(() => !alive(), 'restored helper exited')
      }
    } finally {
      if (parent && parent.exitCode === null) {
        try { execFileSync('taskkill.exe', ['/PID', String(parent.pid), '/F'], { windowsHide: true }) } catch {}
      }
      if (!identity && await exists(path.join(root, 'started.json'))) identity = JSON.parse(await fs.readFile(path.join(root, 'started.json'), 'utf8'))
      if (identity && python) execFileSync(python, ['-I', '-S', '-c', processCheck, sourceHelper, String(identity.Id), identity.Started, 'terminate-owned'], { windowsHide: true, timeout: 15000 })
      await fs.rm(root, { recursive: true, force: true })
    }
  })
}
