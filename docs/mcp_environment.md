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

Pi's direct MCP tools and resource operations ask for Host allow-once/deny even
when a server marks them read-only. This permission is independent of local
workspace write access. Approving a connector operation does not grant local
file writes or shell execution, and cancellation releases pending confirmations.
Pi closes its native session before terminating the process during normal
completion/cancellation. See [Pi integration](pi-rpc-provider.md) for connection
ownership, authentication scope and abnormal-exit limitations.


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
