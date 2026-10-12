/** The desktop and its build tools use the same configured Python runtime. */
import fs from 'node:fs'
import path from 'node:path'

export function projectPythonCommand(projectRoot: string, environment: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform): string {
  const configured = environment.AMADEUS_PYTHON || environment.AMADUES_PYTHON
  if (configured && fs.existsSync(configured)) return configured
  let originalRepo: string | undefined
  try {
    originalRepo = fs.readFileSync(path.join(projectRoot, '.git'), 'utf8').trim()
      .match(/^gitdir:\s*(.+?)[/\\]\.git[/\\]worktrees[/\\]/)?.[1]
  } catch { /* A regular checkout has a .git directory, not a worktree file. */ }
  for (const root of [projectRoot, originalRepo].filter(Boolean) as string[]) {
    for (const relative of ['.venv/Scripts/python.exe', '.venv/bin/python3']) {
      const candidate = path.join(root, relative)
      if (fs.existsSync(candidate)) return candidate
    }
  }
  if (platform === 'win32') {
    const local = environment.LOCALAPPDATA ?? 'C:/Users/' + (environment.USERNAME ?? '') + '/AppData/Local'
    for (const version of ['312', '311', '310', '39', '38']) {
      const candidate = path.join(local, 'Programs', 'Python', 'Python' + version, 'python.exe')
      if (fs.existsSync(candidate)) return candidate
    }
    const store = path.join(local, 'Microsoft', 'WindowsApps', 'python3.exe')
    if (fs.existsSync(store)) return store
  }
  return 'python3'
}
