# 0.17.0 Alpha candidate acceptance

Recorded on 2026-10-14 (Pacific/Auckland). Baseline main:
`7815fdf36480367a1cf57c27e2121379e12967f4`; previous release:
`v0.16.1-alpha.0` (`6147acbc36e2b16faa5d8192a873d40a7feb5965`).
The interval has 16 mainline commits before #208 and release preparation.

## Deterministic and desktop evidence

- Main at the baseline has successful Windows Python/Electron, Linux, macOS,
  local-model installation-candidate and ROCm-candidate CI workflows.
- #208 was reconciled with current main at `cbc8167a2a7f1ff748880cc5975c9b001825f540`.
  Both independent test/documentation additions were retained. Adapter tests:
  72 passed. Adapter plus model-less smoke helper tests in a fresh locked
  CPU/development environment: 78 passed. All 14 current-head checks passed; #208 merged as
  `5b3c68e8f2446efb8e322714f298304d4b7f221a`.
- The tracked-source baseline plus sidebar/smoke changes passed 523 Electron
  tests and production TypeScript/Vite build. The existing large-chunk warning
  remains. Local untracked diagnostic tests were excluded from the clean-source
  run; they are not part of the release.
- Real Electron model-less smoke passed, including expanded/collapsed sidebar
  absence of Plugins, retained Settings -> General -> Manage plugins navigation,
  encrypted settings, native theme recovery, project/file navigation and complete
  Electron/backend exit. The same checks passed on the unpacked Windows executable.
  No LLM turn, microphone, character asset, external MCP or wallpaper mount was used.
- Native previous-release upgrade from `v0.16.1-alpha.0` preserved settings,
  encrypted credentials, independent provider roles and startup preference.
- Tracked-source Ruff, architecture views and provenance release-ready validation
  passed. The initial development-tree source scan had zero scan errors/warnings;
  its dirty status was correctly non-releasable. Final candidate archive checks
  must use a committed clean selected tree.
- The fresh `uv sync --locked --extra dev` CPU environment passed the environment
  verifier. The existing voice/model installation was not replaced.

The initial sandboxed test attempts hit Windows temporary-directory/process
restrictions; the same targeted fixture passed outside that restriction. Those
attempts are not product regressions or passing release evidence. Full local
Python baseline completion and final candidate CI are recorded separately when
available. No final source ZIP is certified by this preliminary record.

## Dependency audit disposition

The clean locked CPU/development environment reported no known vulnerabilities
after the existing narrow `PYSEC-2026-1845` exemption (two advisory identities
ignored). Electron retains eight moderate build-tool findings and no high or
critical findings. Pi 0.86.1 still installs brace-expansion 5.0.9 through its
upstream shrinkwrap: one high-severity package, three denial-of-service advisories.
Targeted `npm update` and `npm audit fix --package-lock-only` did not change this
installed contract; no ineffective lockfile edit or broad suppression was retained.
See the [prior disposition](alpha-0.16-acceptance.md#dependency-audit-disposition).

The existing local environment, which also contains optional model/voice/RAG
packages, reported 17 advisory entries in eight packages (including duplicate
identities), with two ignored identities. This is an audit of that installed
environment, not proof that every version matches the current optional lock.
The affected names were accelerate, flash-attn, hydra-core, nltk,
pytorch-lightning, sentence-transformers, transformers and werkzeug.
No optional-model bulk upgrade or new vulnerability-free claim is made.

## Open PR and issue triage

| Item | Release treatment |
| --- | --- |
| #208 | Merged after 72 adapter tests and all 14 current-head CI checks passed. |
| #199 / #213 | Bounded output retry candidate has 78 focused passes and four before/after shared-state simulations; await maintainer re-review and actual macOS/AirPods evidence before any stronger recovery claim. Not included here. |
| #185 | High-priority Windows wallpaper input/recovery defect. Do not mark fixed or promise reliable wallpaper operation without targeted reproduction/acceptance. |
| #207 | High-priority sequential Codex/MCP ownership defect. Do not conflate approval repair, Work completion and graceful connection cleanup. |
| #201 / #103 | Rendering acceptance and wider-device/resource claims remain open; retain static compatibility and bounded published claims. |
| #194 | Keep extension integration provisional and sidebar hidden; retained code/settings are not a marketplace release. |
| #195 / #196 / #193 | Their implementation PRs #197/#198/#189 are already on main; open design issues do not mean those changes are unmerged. Verify acceptance before closing records. |
| #23 | Earlier comments report the handoff fix verified and PR #36 submitted; the still-open observation issue alone is not proof of a new 0.17 regression. |
| #39 | Defer the transformers 5.x upgrade; its checks fail and local-model compatibility needs separate qualification. |
| #43 / #146 / #98 / #99 / #97 | Defer broad/old provider, memory and continuity branches; #99 explicitly says it is a reference snapshot, not mergeable work. |
| #190 / #142 / #123 / #110 / #64 / #61 | Feature, platform or longer acceptance work; retain the existing scope and avoid treating this release as completion. |
| #176 / #81 / #6 / #4 | Historical release/community tracking; not independent code merge blockers. |

No issue or contributor PR was automatically closed by this triage. Real audio,
AirPods reconnect, sustained wallpaper interaction, multi-monitor behavior,
exclusive MCP cleanup and broader GPU/device qualification remain outside the
model-less acceptance evidence above.

## Sidebar comparison

Isolated empty model-less sessions, with no user conversations or credentials.

![Before: Plugins in the sidebar](evidence/release-0.17.0/sidebar-before.png)

![After: Plugins hidden from the sidebar](evidence/release-0.17.0/sidebar-after.png)
