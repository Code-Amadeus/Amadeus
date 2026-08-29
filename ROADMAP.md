# Product roadmap

This file records the current product direction for Amadeus. It is not a release
schedule or a promise that every candidate will ship. Dated design notes under
`docs/` are evidence and history; this file is the current contributor-facing
summary.

Status terms used below:

- **Active direction**: work may be proposed against an existing product path.
- **Candidate**: plausible future work that still needs a concrete use case and
  design decision.
- **Later**: deliberately outside the source Alpha, but not rejected forever.
- **Not planned**: conflicts with the intended product or current ownership model.

## Current baseline

Amadeus is a local desktop companion with:

- Electron Chat, Projects, Drafts, Artifacts, Settings, Work, wallpaper Slice,
  and an experimental VN Player;
- a local Python server for conversation, voice, visual context, Provider work,
  Work Ledger, AUIP, and render events;
- a CUDA 12.4 local ASR/TTS and llama.cpp profile as the first-release product
  baseline, with CPU/model-less and remote APIs retained as compatibility paths;
- optional external character, model, voice, and visual-runtime packages;
- Provider-scoped MCP and Skills that are not exposed directly to Main Chat;
- a clean CPU/model-less compatibility path that does not require model or
  character packages.

The current product reference uses Windows 11, Python 3.12, Node 22, CUDA 12.4,
an NVIDIA GPU with 8 GiB VRAM, and 16–32 GiB system RAM. Exact local model size
and offload settings still determine peak memory.
Additional desktop platforms are an intended expansion of this baseline rather than a
change to Amadeus's product identity.

## Active directions

### A complete horizontal extension entry

The Host already has a read-only Capability Catalog that projects Providers,
MCP servers, Skills, and AUIP apps without replacing their native contracts.
Settings can manage MCP connections and display shared Provider capabilities.

The next useful product step is a coherent extension entry that can:

- show installation identity, compatibility, health, enablement, and the exact
  Provider or product surface that consumes each contribution;
- add, configure, disable, and remove supported out-of-tree contributions
  without requiring users to find several settings files;
- keep secrets in Host-owned bindings and retain each Provider, MCP, Skill, or
  AUIP runtime as the authority for its native payload;
- prove at least two real out-of-tree adapters before allowing trusted Python
  entry-point discovery.

This direction does not grant plugins action authority and does not attach MCP
tools or Skill prompts directly to Main Chat.

### Source Alpha setup and diagnosability

- Qualify the current cu124 local experience on a clean Windows machine while
  retaining CPU/model-less as the deterministic CI and compatibility contract.
- Make optional model, voice, character, and Provider availability visible in
  Settings instead of failing through missing paths or imports.
- Keep advanced environment settings documented without duplicating several
  conflicting configuration sources.
- Distribute copyright-sensitive and heavyweight runtime assets separately
  while preserving a stable install layout.

### Claude CLI Provider

Claude CLI is an active, planned mainline Work Provider. It will enter through
the existing Provider contract as its own direct transport rather than restoring
the retired Locus gateway.

The integration must preserve the same invariants as Browser and Codex:

- the Host owns Work identity, Project/Draft binding, permissions, Ledger facts,
  Artifacts, and final execution authority;
- Claude CLI owns only its transport-native session and event payload;
- MCP and Skills remain Provider-scoped and are never attached directly to Main
  Chat;
- Settings reports it as unavailable or unregistered until a real executable,
  live caller, cancellation path, and result/artifact contract are verified.

### Project and Artifact continuity

- Keep Draft as the default unbound conversation and Project as an explicit,
  durable context.
- Make project-owned Artifacts easy to reopen, inspect, continue, and discuss
  with Amadeus without treating generated files as hidden chat attachments.
- Preserve provenance and WorkItem relationships so later iteration uses
  verified project state rather than reconstructed conversation prose.

## Candidate directions

### Memory evolution

Amadeus already has bounded conversation working memory and durable event memory
in the Work Ledger. The missing product layer is cross-session recall and a
small, user-governed semantic memory for preferences and long-term context.

If this becomes active work, the intended sequence is:

1. make existing project and Work Ledger events discoverable across sessions;
2. add a small explicit profile plus retrieval, rather than injecting an
   ever-growing transcript into every prompt;
3. give users a way to inspect, correct, forget, and scope retained memories;
4. test persona continuity and bad-precedent propagation before enabling
   automatic memory writing.

A generic vector store or silent prompt dump is not, by itself, a memory
feature.

### Multi-platform support

macOS and Linux are candidates, not currently supported configurations. Work
should progress by portable product layers rather than one large rewrite:

1. qualify the Python server and headless Chat/Work path on clean CI;
2. qualify the ordinary Electron Chat, Settings, Projects, Artifacts, and Work
   surfaces without promising wallpaper or local heavy models;
3. introduce platform audio-device, AEC, launcher, and packaging adapters only
   where real machines and tests exist;
4. keep WorkerW/Lively wallpaper placement, Windows VN launch helpers, and
   CUDA-specific local model scripts as optional Windows integrations.

No platform is described as supported until installation, CI, and a real-device
journey pass there.

### Character-runtime tooling

Amadeus may improve validation, status inspection, and runtime graph diagnostics
for installed character packs. Full animation authoring, source PNG processing,
and graph editing remain SpriteForge responsibilities rather than a second
authoring system inside Amadeus.

### Additional Provider and voice adapters

New Work Providers and ASR/TTS adapters are welcome when they use the existing
contracts, have a real caller, expose availability clearly, and preserve the
model-less baseline. A vendor name alone is not a reason to add a parallel
runtime path.

## Later, after Alpha evidence

- remote extension catalogs, package signatures, automatic updates, rollback,
  and marketplace-style discovery;
- broader public SDK surfaces beyond contracts already exercised by external
  adapters;
- platform-specific wallpaper implementations outside Windows;
- promoting VN Player or another experiment into a supported core surface.

Each requires an Issue and evidence that the simpler local path is insufficient.

## Not planned

- enterprise tenancy, billing, fleet administration, or a commercial-grade RBAC
  console;
- direct arbitrary MCP, Skill, or plugin execution by Main Chat;
- automatic import of untrusted workspace Python as a plugin;
- bundling character IP, reference voices, model weights, authoring PNGs, or
  other restricted runtime media in the source repository;
- restoring PyQt, VTS, GSV Lite, Vox, or legacy wallpaper launchers as the
  primary product path;
- making Docker the primary installation path for the desktop application;
- merging Work and AUIP into one generic execution abstraction;
- provider-specific keyword patches, speculative APIs, or permanent fallback
  layers without a live caller;
- rewriting stable runtime areas only to shorten files or imitate a framework.

## Moving a candidate into active work

A candidate becomes an active direction only after a maintainer-approved Issue
identifies a real user journey, the owning subsystem, the smallest contract
change, compatibility impact, and a deterministic way to verify both success
and failure. Implementation alone does not change roadmap status.
