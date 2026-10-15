/** Host-selected MCP configuration; Pi owns discovery and tool execution. */
import { createMcpExtension, type ExtensionAPI, type LoadedMcpConfig, type McpServerEntry } from '@earendil-works/pi-coding-agent'
import { StdioTransport, StreamableHttpTransport } from '@earendil-works/pi-mcp'

const snapshotKey = Symbol.for('amadeus.pi.hostMcpSnapshot')

type HostMcpConfig = LoadedMcpConfig & { servers: (McpServerEntry & { displayName: string })[] }

function hostSnapshot(): HostMcpConfig {
  // Session replacement reloads extension modules within the same Pi process.
  // Consume the launch environment once; reloads reuse the private process
  // snapshot. A new Work process receives a fresh Host snapshot.
  const owner = process as unknown as Record<symbol, HostMcpConfig | undefined>
  if (!owner[snapshotKey]) {
    const config: HostMcpConfig = JSON.parse(process.env.AMADEUS_PI_MCP_SERVERS || '{"servers":[],"errors":[],"autoEnableCodemode":false}')
    Object.defineProperty(owner, snapshotKey, { value: config })
    delete process.env.AMADEUS_PI_MCP_SERVERS
  }
  return owner[snapshotKey]!
}

export function registerHostMcp(pi: ExtensionAPI): Map<string, string> {
  const config = hostSnapshot()
  const tools = new Map<string, string>()
  if (!config.servers.length && !config.errors.length) return tools

  createMcpExtension({
    loadConfig: () => config,
    // Bound the first-prompt delay; late tools appear on subsequent model turns.
    startupWaitMs: 3_000,
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
      // Native labels retain server/original tool names even when model-facing
      // names have been shortened and hashed. Host display names remain visible.
      const entry = config.servers.find(entry => tool.label.startsWith(`${entry.name}/`))
      const label = entry ? `${entry.displayName} (${entry.name}) / ${tool.label.slice(entry.name.length + 1)}` : tool.label
      tools.set(tool.name, label)
      pi.registerTool(tool)
    },
  })
  return tools
}
