import fs from 'node:fs'
import path from 'node:path'
import { parseEnv } from 'node:util'

/** Development bootstrap input, read before Electron or Python starts. */
export function developmentServer(projectRoot: string, environment: NodeJS.ProcessEnv = process.env) {
  let raw = environment.AMADEUS_DEV_PORT
  if (raw === undefined) {
    try {
      raw = parseEnv(fs.readFileSync(path.join(projectRoot, '.env'), 'utf8')).AMADEUS_DEV_PORT
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
  }
  const value = (raw ?? '5173').trim()
  const port = Number(value)
  if (!/^[0-9]{1,5}$/.test(value) || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('AMADEUS_DEV_PORT must be an integer from 1 to 65535 in the process environment or repository-root .env. / AMADEUS_DEV_PORT 必须是 1–65535 的整数，请检查进程环境变量或项目根目录 .env。')
  }
  return { host: 'localhost', port, origin: new URL(`http://localhost:${port}`).origin }
}

export function isDevelopmentRendererUrl(rawUrl: string, origin: string): boolean {
  try {
    const target = new URL(rawUrl)
    return target.origin === origin && target.pathname === '/'
      && !target.username && !target.password
  } catch {
    return false
  }
}
