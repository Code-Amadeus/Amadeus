# Manual MCP environment bindings

Connection tests and Pi tool calls pass a platform environment allowlist plus the
connection's explicit encrypted environment. This changes existing manual stdio
connections even when no extensions are installed. Version-1 records, commands,
arguments and working directories remain supported.

If a server previously depended on a Host `.env` token or toolchain variable, add
it under **Settings → Providers → MCP → Encrypted environment values**, save and
restart the backend. Test the connection again. Bind only values that server
needs. An unrelated Host credential is not inherited automatically.

The allowlist retains executable lookup, platform directories, locale and
configured proxy/certificate settings. Python import overrides and arbitrary
Host credentials are excluded; explicitly configured bindings take precedence.
This controls accidental inheritance, not the OS access of local executable code.

Codex/ACP native launchers keep their own environment behavior. A native Agent
may succeed with a connection whose Host-owned probe lacks explicit bindings;
configure those bindings rather than treating the two launch paths as identical.
This migration does not add tools or Skills to Main Chat.

A real local stdio regression loads an original manual record, verifies failure
with an ambient-only token, then verifies discovery and a tool call after adding
the explicit binding. It checks cwd and absence of an unrelated Host secret.
Desktop tests verify encrypted persistence and restart projection.
