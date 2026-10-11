import { spawn } from 'node:child_process'
import { once } from 'node:events'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'
import { developmentServer } from '../src/main/developmentServer.ts'

const repositoryRoot = fileURLToPath(new URL('../..', import.meta.url))

export function startupFailure(error, address, platform = process.platform) {
  const code = error.code || error.cause?.code
  const lines = [`[amadeus-dev] ${address ? `Cannot start ${address} / 无法启动开发服务器` : 'Startup failed / 启动失败'}${code ? ` (${code})` : ''}: ${error.message}`]
  if (code === 'EACCES') {
    lines.push('Binding was denied; the port may be reserved by the system. / 监听被拒绝，端口可能被系统保留。')
    if (platform === 'win32') {
      lines.push('Check Windows TCP exclusions / 检查 Windows TCP 排除范围：',
        '  netsh interface ipv4 show excludedportrange protocol=tcp',
        '  netsh interface ipv6 show excludedportrange protocol=tcp')
    }
  } else if (code === 'EADDRINUSE') {
    lines.push('The port is already in use. / 端口已被占用。')
  }
  if (code === 'EACCES' || code === 'EADDRINUSE') {
    lines.push('Choose an available port in the repository-root .env, then restart; for example:',
      '在项目根目录 .env 中选择可用端口后重新启动，例如：',
      '  AMADEUS_DEV_PORT=15173',
      'An existing process environment variable takes priority. / 已设置的进程环境变量优先。')
  }
  return lines.join('\n')
}

/** Own Vite until Electron exits; never launch Electron before listen succeeds. */
export async function startDevelopment({
  projectRoot = repositoryRoot, environment = process.env, withElectron = false,
  electronArgs = [], startElectron,
} = {}) {
  const address = developmentServer(projectRoot, environment)
  const electronRoot = path.join(projectRoot, 'electron')
  let server
  let child
  let closing
  let resolveExit
  const exited = new Promise(resolve => { resolveExit = resolve })
  const close = (code = 0) => {
    closing ??= (async () => {
      if (child?.pid && child.exitCode === null && child.signalCode === null) {
        const stopped = once(child, 'exit')
        // Electron owns backend/wallpaper cleanup. Its disconnect handler also
        // covers a lost terminal or crashed launcher without killing the restorer.
        if (child.connected) child.send('amadeus-dev-quit', error => {
          if (error) console.error('[amadeus-dev] Quit request:', error.message)
        })
        await stopped
      }
      await server?.close()
      resolveExit(code)
    })()
    return closing
  }
  try {
    server = await createServer({
      configFile: path.join(electronRoot, 'vite.config.ts'), root: electronRoot,
      clearScreen: false,
      server: { ...address, strictPort: true },
    })
    // Vite replaces EADDRINUSE with a message-only Error. Preserve the socket
    // error at its owning boundary so diagnostics do not classify English text.
    let bindError
    const rememberError = error => { bindError = error }
    server.httpServer.on('error', rememberError)
    try {
      await server.listen()
    } catch (error) {
      throw bindError ?? error
    } finally {
      server.httpServer.off('error', rememberError)
    }
    if (withElectron) {
      const childEnvironment = { ...environment, AMADEUS_DEV_PORT: String(address.port) }
      if (startElectron) child = startElectron(electronRoot, childEnvironment, electronArgs)
      else {
        const { default: executable } = await import('electron')
        child = spawn(executable, [electronRoot, '--amadeus-dev-launcher', ...electronArgs], {
          cwd: electronRoot, env: childEnvironment, stdio: ['ignore', 'inherit', 'inherit', 'ipc'], windowsHide: true,
          // The cleanup owner must outlive a lost launcher. Windows otherwise
          // kills the child job before Electron can handle IPC disconnect.
          detached: true,
        })
      }
      child.once('exit', (code, signal) => { void close(code ?? (signal ? 1 : 0)) })
      await once(child, 'spawn')
    }
    return { ...address, server, child, close, exited }
  } catch (error) {
    await close(1)
    throw new Error(startupFailure(error, address.origin), { cause: error })
  }
}

async function main() {
  const withElectron = process.argv[2] === '--electron'
  const session = await startDevelopment({ withElectron, electronArgs: process.argv.slice(withElectron ? 3 : 2) })
  console.log(`[amadeus-dev] ${session.origin}`)
  const onInterrupt = () => { void session.close(130) }
  const onTerminate = () => { void session.close(143) }
  process.once('SIGINT', onInterrupt)
  process.once('SIGTERM', onTerminate)
  try { process.exitCode = await session.exited }
  finally {
    process.off('SIGINT', onInterrupt)
    process.off('SIGTERM', onTerminate)
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(error.message)
    process.exitCode = 1
  })
}
