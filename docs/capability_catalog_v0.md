# Capability Catalog v0 — horizontal extension management

Status: implemented static/read-only foundation on `main`.

## Decision

Amadeus manages horizontal extensions through three separate concepts:

```text
Package       installation identity, compatibility, trust, digest, enablement
Contribution  one native capability owned by its existing subsystem
Binding       projection of that capability onto an Amadeus product surface
```

The Catalog is a management and discovery plane. It is not a universal runtime
and does not become a new source of Provider, MCP, Skill, or AUIP payload truth.

## Product invariants

1. Host continues to own identity, durable state, permissions, execution
   authority, receipts, and ledger facts.
2. Provider manifests remain the only Provider selection contract.
3. MCP tool discovery and exact MCP arguments remain owned by the MCP client
   adapter. Work/session metadata is never copied into tool arguments.
4. Skills retain their files and progressive-disclosure behavior. A Skill is a
   capability, not a new personality or speaking authority.
5. AUIP manifests, artifact validation, revision, action payload, attach ticket,
   and AppSession lifecycle remain owned by AUIP. The Catalog stores only an
   opaque revision-bound `artifact_ref`.
6. Work and AUIP remain orthogonal. A catalog contribution cannot create a
   WorkItem or AppSession.
7. Installing or enabling a package never grants an action permission.
8. Secrets are bindings owned by the Host and never package metadata.

## Contribution kinds

| Kind | Native owner | Initial bindings |
| --- | --- | --- |
| `provider` | `ProviderRuntime` manifest registry | `work_execution/provider_manifest` |
| `mcp_server` | MCP client connection and discovered schemas | `work_execution/mcp_provider` |
| `skill` | Skill directory and native loader/stager | `work_execution/agent_skill` |
| `auip_app` | Work artifact registry and AUIP launch validator | `role_chat/auip_launch`, `app_runtime/auip_attach` |

`native_ref` is an opaque lookup identity. The Catalog never embeds MCP input
schemas, an AUIP manifest, application bytes, or a Skill prompt as a substitute
for the native owner.

## Trust and lifecycle

v0 models three trust classes:

- `builtin`: first-party code composed with Amadeus;
- `trusted_local`: local code/artifacts accepted through an existing Host
  verification boundary;
- `external_protocol`: an out-of-process capability reached through a bounded
  protocol such as MCP.

Package enable/disable, removal, and contribution health exist in the in-memory
contract. The first shipping surface is intentionally read-only. It does not:

- scan a workspace for Python entry points;
- import third-party code;
- persist installations;
- download packages;
- implement automatic updates, multiple revisions, or rollback;
- provide a marketplace or dependency solver.

A failed dynamic projection yields one bounded `projection_failed` health signal
and cannot hide healthy built-in capabilities.

## Current composition

At startup:

1. the built-in AUIP authoring Skill is indexed by a path-free digest;
2. the live Provider registry is projected after adapter composition;
3. a Provider whose native runtime kind is `mcp_server` contributes both its
   Provider execution identity and its MCP surface identity;
4. desktop-managed MCP connections are projected from the encrypted local
   registry only to the Work Providers selected by the user;
5. the current conversation's verified AUIP launch candidates are projected
   read-only from `AuipLaunchCoordinator`;
6. `capability.list` returns the combined view and can filter by contribution
   `kind` or product `surface`.

This projection is deliberately not consumed by Provider routing or AUIP launch
in v0, so introducing the Catalog cannot change existing product behavior.

## Cross-Provider rule

MCP and Skills are installed once in the Host. A product-surface or Provider
adapter projects them only when it supports their declared binding and
requirements. Unsupported projections fail explicitly; the Host does not copy
per-Provider configs or silently claim compatibility.

An MCP server is available only through a compatible Work execution Provider.
The Settings registry currently projects enabled connections into the selected
Codex Work Provider by process-local config overrides; it does not rewrite the
user's global Codex config. Main Chat may delegate work and understand its
verified result, but it does not receive the MCP tool catalog or invoke tools
directly. AUIP Participants and Controllers continue to use AUIP typed actions;
an MCP registration grants them nothing.

A later, separately validated adapter may let the same registered MCP server
appear as:

- tools available to a Work execution Provider;
- a standalone MCP-backed Provider operation.

Those execution-side projections may share one installed identity and secret binding, while
their native tool payloads remain unchanged.

## Validation

The v0 regression set covers:

- package identity, duplicate ownership, enable/disable, and health isolation;
- Provider plus MCP dual projection without copying the native tool schema;
- the AUIP authoring Skill's stable digest and path-free metadata;
- exact MCP argument preservation through a real SDK tool call;
- an approved Desktop AUIP bundle retaining the same `export-bundle` identity
  in the Catalog and launch/attach path;
- surface filtering and failure isolation of optional dynamic projections;
- existing Provider selection, authoring, MCP permission, AUIP source, launch,
  attach, B2, and routing suites.

Validation on the implementation commit used the repository's CUDA 12.4 Python
profile and completed the full suite with **1585 passed, 1 skipped**. The run
included the real restricted-WebSocket external AUIP attach E2E. Ruff passed on
all changed Python files, and MyPy passed on the new Catalog, composition, and
handler modules.

## Next phases

Arbitrary executable plugin loading remains blocked until at least two real
out-of-tree adapters pass their native conformance and failure-isolation
journeys. The desktop registry persists connection metadata and encrypts MCP
environment values with Electron `safeStorage`; `Settings > Providers` supports
add, edit, disable, remove, restart-to-apply, and read-only discovery probes.

When that evidence exists:

1. add Provider projection adapters beyond Codex only after their native MCP
   lifecycle and permission boundary is implemented;
2. permit trusted Python entry points only from installed distributions and a
   user allowlist;
3. prefer sidecar/MCP execution for untrusted integrations;
4. add remote catalogs, signatures, upgrades, and rollback only after Alpha.
