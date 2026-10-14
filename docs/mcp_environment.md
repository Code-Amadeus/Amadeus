# Manual MCP connection-test environment

In the current mainline, **Settings → Providers → MCP → Test** is the only
Host-owned consumer of manually configured stdio MCP connections. Its child
process now receives a platform environment allowlist plus the connection's
explicit encrypted bindings, instead of arbitrary Host environment credentials.
A service that relied on a Host token may therefore fail this probe even while
it continues to work through Codex or ACP's independently launched MCP service.

Codex/ACP launch behavior and saved version-1 records, commands, arguments and
working directories are unchanged. Pi does not accept manual MCP connection
bindings on main; its built-in web search uses HTTP and is unaffected by the
stdio environment policy. Later Host-owned MCP consumers (for example extension
execution paths) can use the same helper, but this PR does not introduce them.

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
Host secret; it is not a claim that mainline Pi calls this manual connection.
Desktop tests cover encrypted persistence and restart projection. Nested-error
and Windows/POSIX tests cover redaction, useful causes and override semantics.


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
