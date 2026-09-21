# Experimental Pi daily-task provider

Pi is the default daily-task agent reached through its native stdin/stdout JSONL RPC.
It is not an ACP bridge and does not replace the Python Host. The experimental
runtime is pinned to `@earendil-works/pi-coding-agent@0.86.1`.

## Install and configure

Use Node 22.19 or newer. The normal desktop installation (`npm ci` in
`electron/`) installs the pinned Pi runtime automatically alongside Electron.
An existing desktop checkout can rerun that command to install Pi or restore
the locked version. Pi installation errors fail the desktop installation;
they are not silently ignored. Runtime startup does not install packages.

For a headless deployment or a standalone Pi runtime repair, run this from
the repository root:

```powershell
npm ci --ignore-scripts --prefix agent_host/pi_runtime
```

Pi is enabled by default. The runtime install uses its existing lockfile and
`--ignore-scripts`; if you skip the desktop postinstall with `--ignore-scripts`,
install Pi separately with the command above (and provision Electron separately
for desktop use). Model credentials are still required; installing the runtime
does not configure an account or validate model access.
Settings → Work Provider connections and `.env` expose these defaults:

```dotenv
PI_PROVIDER_ENABLED=true
PI_NODE_PATH=node
PI_AGENT_DIR=runtime/pi
PI_MODEL_PROVIDER=deepseek
PI_MODEL=deepseek-v4-flash
```

Pi uses its native provider authentication: for this example supply
`DEEPSEEK_API_KEY` in the backend environment. Model availability and access are
checked by Pi; the example is not a guarantee of account access. For custom API
endpoints or local models, configure `runtime/pi/models.json` using Pi's native
model schema. Existing Amadeus Models UI endpoint settings are not automatically
translated into Pi configuration. Do not put credentials in command-line args.

For interactive Pi login against the same isolated configuration directory:

```powershell
$env:PI_CODING_AGENT_DIR = (Join-Path (Get-Location) 'runtime/pi')
node agent_host/pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/cli.js
```

Settings → Models → Roles → **Work role assignments** has two independent dropdowns:
Coding defaults to Codex, everyday execution defaults to Pi. Candidates come
from registered compatible manifests, including custom ACP agents. Equivalent
startup settings are:

```dotenv
WORK_CODING_PROVIDER=codex
WORK_EXECUTION_PROVIDER=pi
```

Restart the backend after startup configuration changes. Work Page also exposes
Pi through the existing dynamic provider list. Missing optional runtimes are
reported as unavailable and do not prevent role-only Chat startup. This branch
adds no complexity classifier or automatic failover. Provider selection and
explicit task addressing use the existing planner.

OpenClaw remains registerable through its existing Gateway adapter and keeps
its native session support. It no longer owns the default everyday execution
role by default. It can be assigned the execution role in the GUI, explicitly
selected for an individual task, or used to continue its existing Work.

Role changes update the existing planner's routing instructions and new-task
examples; they do not alter registration, manifests, or existing Work ownership.
Registered general agents remain addressable regardless of role assignment.
No extra requirements JSON is needed to enable an assigned agent: the Host
derives its baseline context requirements from registered capabilities.
`COOPERATIVE_CHAT_REQUIREMENTS_JSON` and
`COOPERATIVE_CHAT_ADDITIONAL_REQUIREMENTS_JSON` remain optional Host policy
overrides, defaulting to `{}`. Remove stale overrides if switching providers
with different workspace capabilities. Legacy `COOPERATIVE_CHAT_PROVIDER` and
`PROVIDER_DELEGATE_DEFAULT_PROVIDER` are read as execution-role migration
aliases only when `WORK_EXECUTION_PROVIDER` is absent.

## Responsibilities and reuse

### Registration and GUI settings

Builtin adapters are composed by `provider_bootstrap.py`; their capability
manifests are code-owned in `provider_catalog.py`. Settings choose connections,
enablement and defaults, not whether an implementation can actually restore,
cancel, access a workspace or accept an MCP projection. The GUI displays runtime
availability; it does not let users grant an adapter unsupported capabilities.

Role dropdowns live under Models → Roles; connection/registration cards live
under Providers, with a link back to the role settings. Changes persist in
Desktop settings and apply at backend restart.
Coding and daily execution route independently; there is no universal Pi role.
The Work page is an explicit per-task provider selector. It initially selects
the execution-role provider and preserves manual choices. An unavailable default
requires a selection rather than silently choosing by manifest priority.

The existing ACP editor adds installed external agents without Python code
changes. Registered compatible ACP agents appear in the role dropdowns too.
Advanced cooperative policy overrides and trusted Pi extension paths remain
startup configuration, with no dedicated GUI editor.
MCP connections are configurable in the GUI only for adapters advertising the
`mcp_connection` projection (currently Codex and compatible ACP agents). Pi's
bundled anonymous web search reuses the MCP client internally; this does not
advertise arbitrary MCP projection support for Pi or OpenClaw.

- `ProviderRuntime`, Work Ledger, control admission and task/session identities
  remain unchanged. Bootstrap registers one ordinary `pi` provider.
- The adapter reuses `ProviderSessionHandle`, `ProviderInputDelivery`, activity
  evidence, parent-context/progress helpers and the Host permission flow.
- `pi_rpc.py` implements only Pi's distinct `type`-based JSONL transport. Codex
  and ACP SDKs own incompatible transports; OpenClaw uses WebSocket RPC. There
  is no second task store or new Host-level agent framework.
- Each run owns one subprocess; the saved native session file is its opaque
  continuation handle. Reattachment requires that exact file and identity.
  Sessions are not reused by guessing the latest transcript.
- Startup update/telemetry requests are disabled for the managed runtime;
  model inference and explicitly invoked network tools still use the network.
- The adapter waits for `agent_settled`, not intermediate `agent_end` events
  which can precede retries, compaction or queued inputs.
- Additional input uses native `steer` acknowledgement at a tool boundary.
  This is append delivery, not the Host's latest-revision-wins plan replacement;
  the manifest deliberately does not advertise immediate replacement steering.
- Cancellation clears queued input and awaits native `abort` acknowledgement.
  Process loss or a missing prompt acknowledgement yields an unknown/orphaned
  execution, never an automatic retry or a claimed successful cancellation.

## Daily tools and native extensions

The bundled `amadeus.ts` extension uses anonymous Exa MCP for search through
Amadeus's existing MCP client, and `pi-simple-web-tools@0.1.0` inside the Pi
process for article extraction only. Its keyed search module is not used.
There is no dependency on Amadeus's Browser provider, planner, browser sessions
or Playwright installation. The extraction package is pinned because the adapter
uses its module API; upgrade it with the integration tests.

| User goal | Tools / existing owner |
| --- | --- |
| Find an article by title, author or topic | `web_search` uses anonymous Exa MCP; no API key or registration |
| Read and summarize an article | `web_read` extracts markdown/HTML/PDF with HTTP and Readability; `web_fetch` reads simple text/feeds |
| Open a supplied or discovered URL visibly | `open_url` reuses the system-browser opener also used by Canvas URL actions |
| Find, read and open a particular article | Pi can compose search → select source → read → open in one Work and continue the same native session |
| Read current news | `news_search` reads Google News RSS; source articles should be read before making detailed claims |

Search connects to `https://mcp.exa.ai/mcp?tools=web_search_exa` only when called.
It requires no API key, browser, local MCP server, or new npm dependency. The
short-lived Python bridge uses the existing `open_mcp_connection` implementation;
it never creates a nested Provider or Work and never reads/sends an Exa key.
There is no paid fallback. The public service has rate limits and may become
unavailable; these produce ordinary tool failures for Pi to handle without
failing the RPC conversation. Queries are sent to Exa as an external search
service. Search relevance and publication dates still need model evaluation.

Live anonymous smoke checks found the original Transformer paper, Chinese
weekly article sources and dated New Zealand news results. Three search calls
took about 0.9–1.1 seconds each, excluding initial connection/process startup.
This is a small sample, not a service guarantee or proof of same-day coverage.

`web_read` returns bounded slices with `next_offset`, so long articles remain
readable without writing outside the Host workspace or granting access to a
global temporary directory. The source URL is the requested URL, not a verified
redirect destination. Content extraction does not prove completeness or bypass
paywalls. Neither search snippets nor news feeds prove the full article was read.

JavaScript rendering is optional. If needed, install Playwright in
`agent_host/pi_runtime` and its Chromium binary; the component loads it lazily.
The default install does not include a browser. This experiment does not provide
general click/type control or access to a user's logged-in tabs.

`pi_web_tools.py` also bridges the shared OS URL opener. A visible open reports
OS acceptance, not verified page load. Opening the system browser does not
create another Provider or Work.

Pi retains its native file/command tools. File tools are confined to the
Host-assigned workspace and access level; command execution and unknown
extension tools require write access and a Host approval. Launching an app uses
an executable plus argv without a shell, always requires approval and reports
process-start evidence only. It does not claim control over an existing app or
reuse Browser login state; those remain with Amadeus's existing domain owners.

Set `PI_EXTENSIONS_JSON` to an array of explicit trusted extension paths to use
the native plugin API. Ambient extension discovery and project trust approval
are disabled; Pi's native skills are retained. Confirmation requests are mapped
to existing Host allow/deny requests. Native select/input/editor dialogs are
cancelled because this provider contract has no general form response; TUI-only
extensions are not supported by RPC.

The extension gate is not an operating-system sandbox: native extensions are
arbitrary code, and approved shell commands have process-user access. Do not
load untrusted extensions or treat the path checks as isolation from hostile
code. No Pi process runs in the Electron UI process.

## Validation

```powershell
.venv/Scripts/python.exe -m pytest tests/test_pi_provider.py tests/test_pi_runtime.py tests/test_pi_web_tools.py
```

The first suite checks acknowledgement uncertainty, correlation, permissions,
cancellation and session identity using deterministic protocol peers. The second
starts the real pinned Pi CLI against a local mock model endpoint to test file
tools, native extensions, permission denial and on-disk session restoration.
It skips when the optional npm runtime is absent; it uses no paid model calls.

For an opt-in real-model run that creates files and opens real browser/Notepad
windows on Windows:

```powershell
.venv/Scripts/python.exe scripts/pi_daily_acceptance.py --provider pi --label trial-1
.venv/Scripts/python.exe scripts/pi_daily_acceptance.py --provider openclaw --label trial-1
```

The harness approves only Notepad opening the exact generated summary file;
it denies additional shell commands. Reports and native sessions stay under
ignored `runtime/pi-acceptance/`. See [live comparison](pi-daily-acceptance.md)
for the observed results and verification limits.

References: [anonymous search](https://github.com/exa-labs/exa-mcp-server#authentication),
[web extraction component](https://github.com/jillesme/pi-simple-web-tools),
[native RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md),
[native extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md).
The installed version's bundled documentation is the protocol baseline.
