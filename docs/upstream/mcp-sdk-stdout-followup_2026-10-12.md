# Draft follow-up: capability diagnostics contaminate a hosting stdio protocol

Not submitted. Proposed destination: existing
[logger issue #1262](https://github.com/modelcontextprotocol/typescript-sdk/issues/1262)
or its [active implementation PR #2370](https://github.com/modelcontextprotocol/typescript-sdk/pull/2370).
Do not open a duplicate issue. Publishing requires the user's permission.

## Problem

A Node application hosting an MCP Client can itself speak ACP/JSON-RPC over
stdout. Calling `listResources()` and `listResourceTemplates()` against a
tools-only MCP server returns the expected empty results but also emits two
`console.debug` lines to the application's stdout. Its outer protocol client
then receives non-JSON frames. Diagnostics should be directed to a configured
non-protocol sink while preserving empty-list behavior.

## Upstream check

Canonical repository: `modelcontextprotocol/typescript-sdk`.
Checked HEAD on 2026-10-12: `b022522089a0c8b632595c6e7b536453945ed5a9`.
The [client implementation](https://github.com/modelcontextprotocol/typescript-sdk/blob/b022522089a0c8b632595c6e7b536453945ed5a9/packages/client/src/client/client.ts#L1551)
still calls `console.debug` in the missing-resource-capability branches
(also present for prompts/tools).

Reproduced on unmodified `@modelcontextprotocol/client@2.0.0` with
`@modelcontextprotocol/server-filesystem@1.0.0`; no application customization or
model is needed. Separately reproduced through unmodified DSH 0.2.0-rc.2 and a
synthetic local model endpoint. A disposable SDK module copy routing those
diagnostics to stderr produced zero non-JSON stdout lines and identical empty
resource results. The original installed SDK was left unchanged.

Issue/PR search found #1262 and #2370; both were open at the check time. PR #2370
head was `1b92074cc7db1cc87be641029436fd8c58ba53fe`, not merged. Its injectable
logger is relevant: the hosting ACP application must configure all levels to
stderr; preserving the default console sink alone would retain this problem.
The PR branch itself was not built or tested here.

## Environment and evidence (sanitized)

Windows x64, Node 22.21.1, Python 3.12 (only for the separate ACP harness),
`agent-client-protocol@0.12.1`, DSH 0.2.0-rc.2, MCP Client 2.0.0.

Minimal reproduction: connect `Client` to an ordinary tools-only stdio MCP
server, then call both resource-list methods. Capture parent stdout separately
from stderr. The two non-JSON lines are emitted before normal results:

```text
Client.listResources() called but server does not advertise resources capability - returning empty list
Client.listResourceTemplates() called but server does not advertise resources capability - returning empty list
```

Both results remain empty and the server need not receive unsupported requests.
The surrounding ACP Python transport reports two JSON parsing errors and
continues. No credentials, conversations, device identifiers or private paths
are required to reproduce. A configured logger must keep diagnostics off the
outer protocol's stdout, including these successful fallback paths.
