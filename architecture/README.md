# Amadeus architecture views

This directory is a code-adjacent snapshot of the current runtime architecture. It is descriptive: the model follows the code that exists now, including compatibility and migration seams, rather than presenting a future ideal.

Snapshot date: 2026-07-22

## Views

- `workspace.dsl` — Structurizr/C4 model of the system, containers, backend components, external providers, and the major runtime relationships.
- `model/states.yaml` — canonical visualization input for WorkItem, RunAttempt, PermissionRequest, and observer narration state.
- `model/permissions.yaml` — current authority/capability boundaries and their guards.
- `model/interactions.yaml` — key provider-work and Desktop-export interaction scenarios.
- `views/*.mmd` — generated Mermaid views. Do not edit these files directly.

Regenerate Mermaid views:

```powershell
python tools/architecture/generate_views.py
```

On Windows, generate the views, build a local overview, and open it in the default browser:

```powershell
preview_architecture.bat
```

The generated overview is written to the git-ignored `runtime/architecture_preview.html`. It loads the pinned Mermaid renderer from `cdn.jsdelivr.net`, so the diagrams need network access when the page is opened. If the CDN is unavailable, the source `.mmd` files remain usable.

Check that generated views are current without writing files:

```powershell
python tools/architecture/generate_views.py --check
```

The generator only reads `architecture/model/*.yaml` and writes `architecture/views/*.mmd`. It does not import or start the Amadeus runtime.

## What the current model makes visible

1. `ProviderRuntime` and the adapter protocol form a real provider-neutral execution boundary.
2. `WorkLedgerCoordinator` owns durable task facts, completion assessment, workspace routing, permission identity, and selected-work projection.
3. `WorkActivityCoordinator` is still a parallel presentation path: it also consumes provider events and emits canvas/work-note activity. This is a migration seam, not a second durable owner.
4. `WorkObserverCoordinator` consumes semantic work notes and owns cadence-gated narration decisions; it does not own provider execution or durable completion.
5. `server.app` remains a large composition root with several callback-based integrations. The diagram marks this as a concentration point without treating it as a domain owner.
6. Canvas actions are untrusted presentation input. Revision, selected WorkItem, selected attempt, capability, workspace, and idempotency checks remain server-owned.

## Scope limits

- These diagrams do not yet extract relationships automatically from Python AST or runtime traces.
- Permissions describe the host-side contract visible in the current code. Provider-internal policy remains provider-owned.
- VN Player, legacy VTS paths, and detailed TTS/ASR internals are grouped at container/component level to keep the first view readable.
- Generated Mermaid files are deterministic, but Structurizr rendering still requires a Structurizr renderer or CLI.
