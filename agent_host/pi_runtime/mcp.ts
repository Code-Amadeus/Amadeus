/** Host-selected MCP configuration; Pi owns discovery and tool execution. */
import { createMcpExtension, type ExtensionAPI, type LoadedMcpConfig, type McpServerEntry } from '@earendil-works/pi-coding-agent'
import { StdioTransport, StreamableHttpTransport } from '@earendil-works/pi-mcp'

const snapshotKey = Symbol.for('amadeus.pi.hostMcpSnapshot')

type HostMcpConfig = Omit<LoadedMcpConfig, 'servers'> & { servers: (McpServerEntry & { displayName: string })[] }
type ApprovalLabel = (input: Record<string, unknown>) => string
const resourceTools = new Set(['list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'])

function approvalLabel(value: string): string {
  // Display only: native tool names and arguments must keep their exact bytes.
  const text = value.replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, ' ').replace(/\s+/gu, ' ').trim()
  const characters = [...text]
  return characters.length <= 320 ? text : characters.slice(0, 319).join('') + '…'
}

function connectionLabel(entry: HostMcpConfig['servers'][number]): string {
  return `${entry.displayName} (${entry.name})`
}

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

export function registerHostMcp(pi: ExtensionAPI): Map<string, ApprovalLabel> {
  const config = hostSnapshot()
  const tools = new Map<string, ApprovalLabel>()
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
      const label = entry ? `${connectionLabel(entry)} / ${tool.label.slice(entry.name.length + 1)}` : tool.label
      tools.set(tool.name, input => {
        if (!resourceTools.has(tool.name)) return approvalLabel(label)
        // Native resource operations choose their server at invocation time.
        // Match Pi's string trimming for display without rewriting the input.
        const server = typeof input.server === 'string' ? input.server.trim() : ''
        const selected = config.servers.find(entry => entry.name === server)
        const scope = selected ? connectionLabel(selected) : server ? `Unknown server (${server})`
          : tool.name === 'read_mcp_resource' ? 'Server not specified'
          : `All bound connections: ${config.servers.map(connectionLabel).join(', ')}`
        return approvalLabel(`${tool.label} / ${scope}`)
      })
      pi.registerTool(tool)
    },
  })
  return tools
}
