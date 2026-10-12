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
