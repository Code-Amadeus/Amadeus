# MCP connection environment and approvals

**Settings → Providers → MCP → Test** and Pi's bound stdio MCP connections
use a platform environment allowlist plus the connection's
explicit encrypted bindings, instead of arbitrary Host environment credentials.
A service that relied on a Host token may therefore fail this probe even while
it continues to work through Codex or ACP's independently launched MCP service.

Saved version-1 records, commands, arguments and working directories are unchanged.
Codex explicitly forwards configured environment names to its stdio MCP children;
ACP launch behavior is unchanged. Pi projects enabled connections bound to it
from the same Host registry, including extension-package contributions. Its
built-in web search uses HTTP and is unaffected by the stdio environment policy.
Pi's native client receives resolved literal values, without its own environment
substitution or command expansion. HTTP bearer bindings use the explicit
connection value before the named Host environment variable; native Pi OAuth
accounts are not imported.

## Migrating a connection test

If a server needs a token or toolchain variable previously supplied by the Host,
add it under **Settings → Providers → MCP → Encrypted environment values**, save
and restart the backend, then test again. Bind only values that server needs.

Probe errors show the underlying exception(s), with configured credentials
redacted, instead of a generic TaskGroup wrapper. Stdio failures also point to
the encrypted environment settings. This is troubleshooting guidance, not a
claim that every connection error is caused by missing credentials: executable,
working-directory and protocol failures still show their underlying reasons.
The instruction is translated in the Chinese UI. HTTP failures do not receive
a stdio migration hint.

The allowlist retains executable lookup, platform directories, locale and
configured proxy/certificate settings. Python import overrides and arbitrary
Host credentials are excluded. Explicit bindings take precedence, including
case-insensitive names on Windows; POSIX names remain case-sensitive. This
controls accidental inheritance, not the OS access of local executable code.

## Evidence

A real local stdio regression loads an original manual record bound to Codex,
verifies an actionable probe failure with an ambient-only token, then verifies
discovery after adding an explicit binding. A direct call through the shared
Host helper verifies the bound child environment, cwd and absence of an unrelated
Host secret. Pi's real-runtime tests separately exercise bound stdio/HTTP calls,
literal credentials, isolation, resources, approvals and native session cleanup.
Desktop tests cover encrypted persistence and restart projection. Nested-error
and Windows/POSIX tests cover redaction, useful causes and override semantics.

## Pi tool approvals

By default, Pi's direct MCP tools and resource operations ask for Host allow-once/deny even
when a server marks them read-only. This permission is independent of local
workspace write access, including project-bound read-only cooperative chats.
Pi and the standard MCP Provider use `mcp.tool.call` / `call_tool` for tool-call
approval. Codex's server input requests retain the separate `mcp.elicitation`
contract. Direct-tool cards retain the connection display name and original tool
name; resource cards resolve the selected server's display name from the Host
snapshot. Unfiltered listings explicitly identify all bound connections. Labels
are normalized to one bounded line without changing execution identifiers or
arguments.
Approving a connector operation does not grant local
file writes or shell execution, and cancellation releases pending confirmations.
Pi closes its native session before terminating the process during normal
completion/cancellation. See [Pi integration](pi-rpc-provider.md) for connection
ownership, authentication scope and abnormal-exit limitations.

For a Pi call bound to one known connection, the Work page and Slice permission
card also offer **Allow identical calls for this Work**. This explicitly grants
only that connection/configuration, original tool identity/schema and the exact
argument values. It does not trust `readOnlyHint`, create a wildcard tool grant,
or authorize a different operation on a mixed query/action tool. Aggregate
resource listings and unknown connections retain per-call approval. Active
grants remain visible and can be revoked individually; revocation affects the
next approval boundary, not an operation already dispatched. Grants expire
when the current Provider run finishes, fails or is cancelled, including when
the same native session is later restored for another Work. Every reuse has a
Host audit event; the original user decision remains immutable.

**Settings → Providers → Work approval → Automatically approve all Work
execution** is a separate persistent switch, off by default. Enabling it
automatically answers supported Amadeus execution confirmations for every Work
and Provider, including MCP and shell. It also answers confirmations already
waiting. Desktop settings retain the switch across restarts; turning it off in
Settings or the Work page restores approval for subsequent requests. Each
automatic decision is recorded. This setting does not add workspace access,
answer arbitrary MCP forms, replay a failed/unknown operation or remove an
external tool's own confirmation rules.

中文：默认仍逐次审批。Pi 的确切连接调用可以选择“本次 Work 允许相同调用”，
只复用相同连接配置、工具及完整参数；授权可见、可逐条撤销，当前运行结束或取消即失效。
“设置 → Providers → Work 审批 → 自动批准所有 Work 执行”是独立的持久开关，默认关闭，
开启后自动批准所有 Work 的 Amadeus 执行确认（包括 MCP 和 Shell），直到手动关闭。
开关会处理当前等待的确认并跨重启保存；每次批准与授权复用均留痕，外部工具自身门禁不变。


## Codex MCP confirmation requests

Codex App Server can request an MCP tool confirmation through
`mcpServer/elicitation/request`. The adapter preserves the server and message
in the existing Work permission card. For a supported empty object form,
Allow once returns `action: accept, content: {}`; denial, expiry or cancellation
returns `action: decline, content: null`. Each pending request has its own ID.
This is not a sandbox permissions grant and does not bypass the Runtime's
input opt-in or action gates.

This permission UI does not collect arbitrary MCP form values or complete URL
authorization: those requests cannot be accepted here. Unknown Codex server
request methods fail explicitly rather than becoming sandbox approval cards.
Existing workspace and Host permission policies still apply.

中文：Codex 的 MCP 空表单确认由原有 Work 权限卡处理，保留服务名和原问题；
允许、拒绝、超时和取消按 MCP 的 action/content 合同回复。任意字段表单和 URL
授权尚不支持，不能用一次“允许”伪造答案；未知协议方法明确报错，不映射为沙箱权限。
Runtime 输入选择和动作门禁、Host 原有权限策略继续有效。

## Explicit bindings through Codex

For a manually bound stdio connection, the Codex adapter supplies the configured
environment variable names through the native per-server `env_vars` option.
Their values remain in the provider process environment and are not written into
command-line overrides. Only names explicitly configured on that connection are
added; this does not restore arbitrary Host-environment forwarding. HTTP
connections and their bearer-token binding are unchanged.
