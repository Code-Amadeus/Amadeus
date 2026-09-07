import fs from 'fs'
import path from 'path'

type PythonRuntimeOptions = {
  projectRoot: string
  platform?: NodeJS.Platform
  environment?: NodeJS.ProcessEnv
  exists?: (candidate: string) => boolean
  readText?: (candidate: string) => string
}

function resolveOriginalRepo(
  projectRoot: string,
  exists: (candidate: string) => boolean,
  readText: (candidate: string) => string,
): string | null {
  try {
    const gitFile = path.join(projectRoot, '.git')
    if (!exists(gitFile)) return null
    const content = readText(gitFile).trim()
    const match = content.match(/^gitdir:\s*(.+?)[/\\]\.git[/\\]worktrees[/\\]/)
    return match?.[1] ?? null
  } catch {
    return null
  }
}

export function resolvePythonCommand({
  projectRoot,
  platform = process.platform,
  environment = process.env,
  exists = fs.existsSync,
  readText = candidate => fs.readFileSync(candidate, 'utf-8'),
}: PythonRuntimeOptions): string {
  const configured = environment.AMADEUS_PYTHON || environment.AMADUES_PYTHON
  if (configured && exists(configured)) return configured

  const originalRepo = resolveOriginalRepo(projectRoot, exists, readText)
  const roots = [projectRoot, originalRepo].filter(Boolean) as string[]
  const venvNames = ['.venv']
  for (const root of roots) {
    for (const name of venvNames) {
      const candidates = platform === 'win32'
        ? [path.join(root, name, 'Scripts', 'python.exe')]
        : [path.join(root, name, 'bin', 'python3'), path.join(root, name, 'bin', 'python')]
      for (const candidate of candidates) {
        if (exists(candidate)) return candidate
      }
    }
  }

  if (platform === 'win32') {
    const condaRegistry = environment.USERPROFILE
      ? path.join(environment.USERPROFILE, '.conda', 'environments.txt')
      : ''
    if (condaRegistry && exists(condaRegistry)) {
      const projectName = path.win32.basename(projectRoot).toLowerCase()
      try {
        const matchingEnvironment = readText(condaRegistry)
          .split(/\r?\n/)
          .map(candidate => candidate.trim())
          .find(candidate => path.win32.basename(candidate).toLowerCase() === projectName)
        if (matchingEnvironment) {
          const interpreter = path.join(matchingEnvironment, 'python.exe')
          if (exists(interpreter)) return interpreter
        }
      } catch {
        // A stale or unreadable Conda registry should not block other options.
      }
    }

    const localAppData = environment.LOCALAPPDATA
      ?? `C:/Users/${environment.USERNAME ?? ''}/AppData/Local`
    for (const version of ['313', '312', '311', '310', '39', '38']) {
      const candidate = path.join(localAppData, 'Programs', 'Python', `Python${version}`, 'python.exe')
      if (exists(candidate)) return candidate
    }

    // Do not select the WindowsApps python aliases by file presence: those
    // placeholder executables can exist while no Store Python is installed.
    return 'python'
  }

  return 'python3'
}
