# ACP Work Providers — experimental

Status: experimental, opt-in ACP v1 integration for 0.15 Alpha. It has not been
integrated into real production use. Codex remains the existing default coding Provider.

Claude's earlier setup below is a historical local example. DeepSeek Harness
0.2.0-rc.2 additionally has real-model and Electron/Chat/Work extension acceptance
on the experimental branch. These establish bounded behavior, not production
qualification or daily-use reliability; see the current constraints below.

Amadeus reuses `agent-client-protocol==0.12.1` for stdio framing, typed
messages, notification draining, and subprocess lifetime. `AcpProviderAdapter`
translates that protocol into the existing Provider request/event/result
contract. DeepSeek Harness and Claude run externally and own their agent loops,
tools, native sessions, and native execution policies.

```mermaid
flowchart TD
    Host[Host Work admission and workspace] --> Runtime[ProviderRuntime]
    Runtime --> Native[Existing native Providers]
    Runtime --> ACP[AcpProviderAdapter: one instance per configured Agent]
    ACP --> SDK[Official Python ACP SDK]
    SDK --> DSH[DeepSeek Harness process]
    SDK --> Claude[Claude Agent ACP process]
    ACP --> Events[Existing canonical Provider events and results]
    Native --> Events
    Events --> Ledger[Work Ledger and Host outcome verification]
    Ledger --> UI[Work UI, narration, user acceptance]
```

## Setup

Install the optional Host dependency in the Python environment that runs Amadeus:

```powershell
python -m pip install -e ".[acp]"
```

The dev extra/CI lock includes it so deterministic ACP tests run in CI. The
default CPU runtime does not require the SDK or any external agent installation.

Install an Agent separately, following its upstream instructions. The initial
native probes on 2026-09-13 used these npm packages in an isolated directory.
These are the historical example versions, not a claim about current releases:

- `@deepseek-ai/dsh@0.1.5-rc.1`
- `@agentclientprotocol/claude-agent-acp@0.76.0`

Settings → Providers → ACP agents offers DeepSeek, Claude, and custom entries.
Choose a unique id, an executable, and separate arguments; enable the entry,
save, and restart the backend. An Agent is registered only when its local
executable, SDK and required environment references are available. Registration
does not certify authentication or protocol capabilities: those are checked
when the Agent connects. Existing Agents and the model-less Host still start
when an ACP entry is unavailable.

On Windows use `node.exe` with the installed JavaScript entry point. The Host
does not invoke `.cmd`/`.bat`/PowerShell launchers through a shell or automatically
install packages. Example argument lists:

```text
DeepSeek:
C:/agents/node_modules/@deepseek-ai/dsh/lib/bin.js
--profile
acp

Claude:
C:/agents/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js
```

For headless startup, the same configuration is supplied in
`AMADEUS_ACP_PROVIDERS` (JSON array). This is the one registry for both entry
points; it is not discovered from the delegated workspace:

```json
[
  {
    "id": "deepseek",
    "name": "DeepSeek Harness",
    "command": "node",
    "args": ["C:/agents/node_modules/@deepseek-ai/dsh/lib/bin.js", "--profile", "acp"],
    "enabled": true,
    "skill_discovery": "dsh",
    "resume": true,
    "environment": {"DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY"},
    "config_options": {}
  },
  {
    "id": "claude",
    "name": "Claude",
    "command": "node",
    "args": ["C:/agents/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js"],
    "enabled": false,
    "resume": true,
    "environment": {},
    "config_options": {}
  }
]
```

`environment` maps the child environment variable to the **name** of a Host
environment variable. Its values are never credentials. Secrets can be stored
using the existing encrypted Desktop settings (DeepSeek/Anthropic keys), supplied
in the backend environment, or obtained through the Agent's existing login.
Omit an API-key reference when using an existing native login. The SDK inherits
only its standard OS environment allowlist; other Host credentials and MCP
bindings are not copied into the Agent process.

Use an explicit `DSH_HOME` reference if you want a dedicated DeepSeek profile.
This is an Agent-owned configuration location, not a second Amadeus Project.

## Models and capabilities

The first native session returns its `configOptions`. Settings can refresh and
display these choices after that connection. Saved selections apply when a
subsequent run opens its session, after the backend restart. Explicit overrides
also accept `option-id=value` entries; unsupported ids/values fail before prompt
submission. Changing model-dependent options uses each complete updated native
configuration response. No model catalog is fabricated from vendor names.

An enabled persistent-session setting requires `session/resume` or
`loadSession` in the native handshake. An unsupported or stale attachment fails;
it never creates a fresh session or silently resubmits an old prompt. ACP v1's
advertised optional resume/close methods are used through the official SDK.
The adapter does not advertise active append-input, immediate steering, or
submission reconciliation. Installed Skills default to a task-level metadata
catalog (`skill_discovery: "catalog"`). Set `skill_discovery: "dsh"` explicitly
for DeepSeek Harness native discovery. The Host adds a temporary final DSH
startup overlay for the selected installed Skill directories, while DSH owns
registration, body loading and its independently configured native plugins.
This is a DSH launch integration, not an ACP Skill-install RPC. Save and restart
to apply either selection. Changed Skill revisions reject old native sessions;
backend restart alone does not make old instructions disappear.

The existing MCP settings can bind connections to the individual ACP Provider
ids. Only enabled bindings for that Provider are projected. HTTP support is
checked at initialization. ACP v1 has no per-server cwd field, so a different
configured stdio MCP cwd is rejected before starting the prompt rather than
discarded. Host MCP tools remain inaccessible to Main Chat.

## Lifecycle and authority

- Each run uses one SDK-managed process. There is no second scheduler or daemon.
  A completed session may be persisted by the native Agent and explicitly
  attached by the Host on a later run; the process is not kept alive for idle UI.
- The Host chooses cwd and owns Project, WorkItem, Attempt and run ids. The
  typed Provider session is checkpointed before submitting the user's prompt.
- Tools execute inside the external Agent under its configured native policy.
  ACP is not an OS sandbox and does not standardize a cross-Agent read-only
  sandbox. An Agent installation must be trusted for its configured execution
  scope; this adapter does not claim Codex sandbox parity.
- Native tool requests enter the existing Host permission service. An allow-once
  decision selects only an ACP `allow_once` option, never `allow_always`.
  Replies must bind the current run and pending request. Timeout, cancellation,
  and shutdown expire pending requests. Agent modes cannot grant Host product
  permissions such as export or AUIP launch.
- A cancellation write or process exit cannot prove cancellation. Only a native
  terminal acknowledgement (or cancellation before submission) confirms it.
- EOF, a missing terminal acknowledgement, malformed/lost event projection, or
  unconfirmed timeout after submission yields an orphaned unknown outcome. It
  cannot authorize automatic retry/resume. ACP `auth_required` is an explicit
  admission rejection and yields an actionable failure instead.
- Native text/tool activity is evidence. Host result verification, artifact
  provenance, Work completion and user acceptance retain their existing owners.

## Verification

`tests/test_acp_provider.py` uses actual SDK connections and separate test Agent
processes, without a model. It covers input/terminal ordering, configured models,
credential isolation, distinct Provider identity, native attachment, approvals,
cancellation, EOF, malformed notifications, slow Host projection, timeout and
shutdown. A real Runtime/Store/Coordinator case verifies durable Work identity
and that Agent prose cannot create artifacts or user acceptance.

`electron/tests/acpSettings.test.mjs` verifies persistence, encrypted credential
separation, environment precedence, validation and MCP bindings for configured ids.

The current real-model extension probe is opt-in:

```powershell
python tools/probes/probe_dsh_extension_live.py --help
```

See [the extension Host profile](extension_host_profile_v0.1.md) for supported
contributions and limits. The probe's help describes its disposable inputs and phases. Running a phase
uses real inference and tools. It does not create or complete a Host WorkItem.
The older `probe_acp_provider.py` referred to by historical notes is not present
in this checkout.

### Historical local examples (2026-09-13, Windows)

| Agent | Example setup | Bounded observation |
| --- | --- | --- |
| DeepSeek Harness (dsh), `@deepseek-ai/dsh@0.1.5-rc.1` | `node` with `lib/bin.js --profile acp`; Host environment references supply the Agent's credentials | Negotiated ACP v1, completed a real model reply and returned a native session. |
| Claude, `@agentclientprotocol/claude-agent-acp@0.76.0` | `node` with `dist/index.js`; existing official Claude Code CLI login, without an API-key override | First returned authentication-required; after login, negotiated ACP v1 and completed a real model reply through the ACP/Agent SDK path. |

These were disposable local integration probes. Neither example has been integrated
into real production use, and neither is a complete release or human UI acceptance.

Both Agents also passed a two-process continuity probe (the resumed session
recalled a marker from the preceding process), application of an advertised
model selection, and a real native file-read task whose output matched the
disposable input file. Canonical `tool.call`/`tool.result` and progress events
were observed for both. The settings form was rendered and its edit/save/model
selector behavior checked in a browser with mocked Desktop IPC; this does not
claim a full running-Electron end-to-end journey.

## Current DSH constraints and diagnostics (2026-10-12)

`@deepseek-ai/dsh@0.2.0-rc.2` uses `@modelcontextprotocol/client@2.0.0`.
When the client lists resources/templates on a tools-only MCP server, two SDK
`console.debug` diagnostics reach the ACP stdout channel. The ACP Python SDK
logs parse errors and continues; successful tools therefore do not prove a
clean wire. This was reproduced without an external model or credentials:

```powershell
python tools/probes/probe_dsh_stdio.py
node tools/probes/probe_mcp_stdout.mjs
```

The second command compares unmodified upstream code with a disposable module
copy that routes diagnostics to stderr; the original installed SDK is unchanged.
Track the [existing SDK logger work](https://github.com/modelcontextprotocol/typescript-sdk/pull/2370).
There is no production Host line filter, global console override, or Agent
package patch. Keep this runtime combination experimental until the corrected
dependency/integration passes the clean-wire probe.

On Windows, workspace-write sandbox setup needs `WRITE_DAC` for its allowlist
and `WRITE_OWNER` for the Low integrity label. Being able to write files or
being the directory owner is insufficient. Check the effective access of an
existing directory without changing it:

```powershell
python tools/probes/check_windows_acl.py <workspace>
```

`probe_dsh_acl.mjs` and `probe_dsh_acl.mjs temp` are opt-in sandbox experiments:
they create their own test directory, initialize the real DSH sandbox, test
inside/outside writes if setup succeeds, then clean up their files. They do
not repair existing workspaces or change the configured project path.

The observed checkout-local failure is missing `WRITE_OWNER`; a fresh directory
under the user's temporary root has both rights and passed inside-write and
outside-write-denial tests. Use an explicitly chosen workspace whose permissions
meet the native sandbox's requirements, or have its owner review the ACL outside
Amadeus. Do not automatically take ownership, broadly grant rights, suppress the
label operation or fall back to unrestricted execution. An initialization error
is not proof that a model honored a natural-language no-shell instruction.

## Upstream references

- [Official Python SDK and lifecycle examples](https://github.com/agentclientprotocol/python-sdk)
- [DeepSeek's automation ACP contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md)
- [Claude Agent ACP bridge](https://github.com/agentclientprotocol/claude-agent-acp)
- [ACP v1 configuration](https://agentclientprotocol.com/protocol/v1/session-config-options)
- [ACP v2 migration and draft boundary](https://agentclientprotocol.com/protocol/v2/migration)

Reuse focuses on the SDK's connection and cleanup owners and the Agents'
capability-driven session/configuration behavior. No upstream implementation
is copied into the Host, and v2 changes remain a future adapter concern.
