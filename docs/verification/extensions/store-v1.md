# Plugin store technical preview: release checklist

Status: local S3 preparation, **not released and not approved for publication**.
The experimental branch implements D1/D2 and S0–S2. D0's cross-repository author link,
D3 external-author qualification and the remaining S3 gates below are still open.
No public index, release, repository permission or main branch was changed.

## Locally available evidence

- [S1 transport and durable safety](community-preview-batch3-2026-10-21.md):
  system proxy/certificates, mirrors, download failures, cancellation, wrong digest,
  revocation, settings NUL corruption/backup recovery and fail-closed double damage.
- [S2 discovery and manual update](community-preview-batch4-2026-10-21.md):
  27 real-wheel UI checks, literal third-party text, unsupported future versions,
  copyright withdrawal and lifecycle/data retention. These use fixture authority.
- [S3 construction record](community-preview-batch5-2026-10-21.md): packaged checker
  closure, actual shipping-shell journeys, fixed old readers and source archive checks.
- [Pi/dependency remediation](community-preview-batch7-2026-10-21.md): original
  Pi assertions/deadlines retained, headless Qwen packaging, patched image/TLS
  dependencies, full local-model import checks and updated review package.
- [Premerge review and isolated qualification](community-preview-batch6-2026-10-21.md):
  fresh Python/Node dependencies, source ZIP, Windows package, Docker protocol/SDK,
  shipping failure recovery and separately labelled native UI operator evidence.
- Four reference families (Tone TTS, Notes MCP, Handoff Skill, desktop theme), each
  at 0.1.0 and 0.2.0, have static checker/policy evidence and full wheel-content
  inventories in the protocol experiment's `catalog/review-candidates/`.
  All eight are **not reviewed**. The 0.1.0 to 0.2.0 diff compares candidates,
  not previously approved versions. No publisher or reviewer identity is invented.

The shipping-shell journey uses normal backend authentication and production
renderer/preload/IPC. It exercises resource install, Appearance selection, mirror
update, offline management, process install, real Runner calls and Notes save/read/list through the native MCP client.
The Pi binding is selected in the UI; no model-driven Agent turn is claimed. The desktop
package remains a frontend with an explicitly supplied Python backend/runtime;
this does not claim a self-contained installer or a clean-machine certification.
A synthetic pre-catalog settings file demonstrates additive settings compatibility,
not restoration of arbitrary old user credentials or profiles.

## Required before a public technical preview

| Gate | Current state | Concrete completion evidence |
| --- | --- | --- |
| Host regression and dependency audit | Batch 6 blockers locally remediated; remote CI pending | Batch 7: 197 pass / 7 existing conditional skips; original Pi assertions and 3s/7s startup bounds retained; five independent focused batches all pass. certifi/Pillow findings cleared under the unchanged CI audit policy. Core and local-cpu dependency checks pass; the existing pytest exceptions and separate model/GPU qualification limits remain explicit. |
| Independent artifact approval | Pending | Non-author maintainer reviews each exact wheel against curated-v1, confirms ownership and asset rights, records digest and evidence in an approved input PR. At least one useful process package and one resource package. Tone is only a synthetic PCM reference; Notes MCP is a candidate useful process package. |
| Public immutable artifacts | Pending | Approved versioned wheel URLs with matching SHA-256 and retained review inventory; never replace bytes at an existing ID/version. |
| Public checker/generator pins | Pending | Reviewed commits reachable from the intended public repositories; fixed Windows/CPython 3.12.10 catalog CI passes using the recorded Host checker and minimal lock. |
| Main protection | Failed configuration gate on 2026-10-21 | Branch protection or active ruleset requires non-author approval and necessary checks; direct and force pushes are restricted. GitHub returned “Branch not protected” and no applicable branch rules. |
| Maintainer two-factor authentication | Unverified | Organization requires 2FA or maintainers provide appropriate administrative verification. The API reports organization enforcement false; that does not prove individual maintainers lack 2FA. |
| Workflow privilege | Partially verified | Repository default token permission is read, Actions cannot approve PRs. Recheck final workflows and environment/release access before publication; submitted code gets no publishing credentials. |
| Clean source and packaged Windows journeys | Local isolated qualification available; clean-machine/independent human acceptance pending | Fresh supported Windows environment, documented supplied Python dependencies, no old profile/install/cache; useful process + resource package through full lifecycle, manual native confirmations separately recorded. Local automated shell evidence alone is insufficient. |
| Full release failure matrix | Local final-build fixture matrix recorded in batch 6; reviewed public catalog still pending | Re-run cancellation, digest mismatch, withdrawal, ban, double corruption, startup failure, data retention/delete on final fixed release build and reviewed catalog. Record screenshots, traces, skips and hashes. |
| Remote CI and release resources | Local checks only | Required remote Actions green on final target commit; archive/desktop hashes, SDK/catalog pins, exact catalog revision and plugin identities recorded. |
| First catalog publication/withdrawal | Pending | Reviewed input PR, separate publication PR with sequence +1, exact reproducible generation; withdrawal and security decision PR exercise, offline known bans retained. Content rollback must still increase sequence. |
| D0 public author path | Pending Host integration | After reviewed Host integration, protocol README points to the immutable integrated author guide. Do not point a public completion claim at a local-only commit. |
| Release decision | Pending | Maintainer explicitly authorizes publication after evaluating the above evidence. Passing tests never publishes automatically. |

Maintainer reference packages may form the first catalog after independent review.
D3 can remain separate, but the public description must then say **external-author
validation is not complete**. Two external authors' consented lifecycle evidence is
required to close community qualification; it cannot be substituted by these fixtures.

## Withdrawal operation

Use the protocol repository's `catalog/README.md` publication procedure and
`catalog/REVIEW_CHECKLIST.md`. Ordinary retirement and copyright claims update
`withdrawal` with the appropriate reason. A security block adds/updates the durable
exact ID/version/SHA-256 decision with the next sequence; retain all prior known
identities. Unblocking is an explicit higher-sequence decision. Regenerate from
approved input with a reviewed generator, compare against protected main, and merge
only after review and CI. No workflow should push a result directly to main.

Clients learn new decisions only when users browse or explicitly check security.
Offline clients retain already known bans; never checking means no new remote
protection. A copyright withdrawal stops distribution without becoming a security
ban. Existing local management remains available; no automatic rollback or data
restoration is promised.
