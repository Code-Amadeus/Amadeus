/** Host-selected MCP configuration; Pi owns discovery and tool execution. */
import { createMcpExtension, type ExtensionAPI, type McpServerEntry } from '@earendil-works/pi-coding-agent'
import { StdioTransport, StreamableHttpTransport } from '@earendil-works/pi-mcp'

const snapshotKey = Symbol.for('amadeus.pi.hostMcpSnapshot')

function hostSnapshot(): McpServerEntry[] {
  // Session replacement reloads extension modules within the same Pi process.
  // Consume the launch environment once; reloads reuse the private process
  // snapshot. A new Work process receives a fresh Host snapshot.
  const owner = process as unknown as Record<symbol, McpServerEntry[] | undefined>
  if (!owner[snapshotKey]) {
    const entries: McpServerEntry[] = JSON.parse(process.env.AMADEUS_PI_MCP_SERVERS || '[]')
    Object.defineProperty(owner, snapshotKey, { value: entries })
    delete process.env.AMADEUS_PI_MCP_SERVERS
  }
  return owner[snapshotKey]!
}

export function registerHostMcp(pi: ExtensionAPI): Set<string> {
  const entries = hostSnapshot()
  const tools = new Set<string>()
  if (!entries.length) return tools

  createMcpExtension({
    loadConfig: () => ({ servers: entries, autoEnableCodemode: false, errors: [] }),
    createTransport: ({ config }) => {
      // Host values are already resolved: do not interpret ${...} or !commands,
      // inherit Pi's model credentials, or import Pi's native OAuth accounts.
      if ('url' in config) return new StreamableHttpTransport({ url: config.url, headers: config.headers })
      return new StdioTransport({ command: config.command, args: config.args, cwd: config.cwd,
        env: config.env, inheritEnv: false, stderr: 'pipe' })
    },
  })({
    ...pi,
    // Host settings own connection changes and authentication. Native extension
    // registrations and the interactive /mcp manager cannot add another source.
    getMcpServers: () => [],
    registerCommand: () => {},
    registerTool: tool => {
      tools.add(tool.name)
      pi.registerTool(tool)
    },
  })
  return tools
}
