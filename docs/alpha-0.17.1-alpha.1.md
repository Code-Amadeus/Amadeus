# 0.17.1 Alpha 1: bundled dictionary and plugin store candidate

Candidate tag: `v0.17.1-alpha.1`; Python `0.17.1a1`; Electron `0.17.1-alpha.1`.
This is a source Alpha release candidate, pending combined qualification and an
explicit publication decision. It does not provide a self-contained Python/Electron
installer, model weights, character media, personal dictionaries or credentials.

## Correction to the initial preparation

[PR #245](https://github.com/Code-Amadeus/Amadeus/pull/245) prepared Alpha 0 against
`fd17e707`, with the separate 2026.10.20.3 dictionary (129 rows / 127 words).
That tag already exists and its Release has a prior publication timestamp even
though it is currently a draft. Keep the tag and old bytes unchanged. Alpha 1
has a new identity and is qualified from current main; Alpha 0 artifacts must
not be relabeled or substituted for this candidate.

The [Alpha 0 notes](alpha-0.17.1.md) remain historical. In particular, their
optional-only delivery instructions and small-dictionary test counts do not
apply to the full resource below. The AEC repair from
[PR #249](https://github.com/Code-Amadeus/Amadeus/pull/249) is included in Alpha 1.

## Default dictionary included in the application

Full application source archives and Electron resources include all nine exact
members selected by `release/japanese_dictionary.json`. The data remains the
independently versioned `2026.10.22.1-rc2` candidate; this release does not promote
it to a fully reviewed or pronunciation-certified data version.

- Independent archive: `amadeus-japanese-pronunciation-2026.10.22.1-rc2.zip`
- Size: 37,386,096 bytes
- SHA-256: `a935b3adfded9022afe3b2384cebc2ef1e4023f54f48df1f9ded9ff45b896989`
- Resource identity: `298576bfaf6e553f55631b2bbbea8099deb9b3919903bce8f646ca2ecd791c46`
- Full source denominator: 134,201 rows; preserved source rows are not all
  individually verified word/reading pairs.

A fresh full-source extraction has the bundled default available without a
separate dictionary download. An explicit independently installed selection
continues to win, including existing format-1 installs; application upgrades
must not silently replace that selection. Independent updates/rollback remain
available. Personal `assets/text/ja/user.csv` and saved settings are preserved,
and compiled caches stay outside the bundled resource directory. See
[dictionary behavior and return to the bundled default](japanese_pronunciation_dictionary.md).

All source/license notices travel with the exact resource. The optional context
reading model remains separate and disabled by default; it is not distributed
with this candidate. Missing or mismatched dictionary bytes fail a full build.
Code-only CI/developer artifacts cannot be published as complete application
archives.

## User-visible changes in the candidate

- The plugin store supports official-catalog browsing, artifact details,
  installation progress/cancellation, manual updates, HTTPS artifact mirrors,
  withdrawal and persistent security blocking/unblocking with explicit recovery.
  The current official catalog is empty and displayed as a preview. Nothing
  automatically updates or promises rollback. Safety checks happen on explicit
  browsing/check actions; a user who never checks cannot receive new remote bans.
- Installed plugins use the integrated Plugins page, package switches,
  configuration drafts, details/removal and local manifest-v1 icons. The current
  catalog checker still excludes v1 icon packages pending its separate
  compatibility qualification; local runtime support does not imply catalog admission.
- Pi is credited in Related projects as the default daily-task agent, using
  its pinned native RPC runtime. Complex coding keeps the existing Codex path.
- Dependency maintenance includes Pillow 13, patched certifi and OpenTelemetry
  API, and the narrowly repackaged Qwen-ASR dependency metadata. Existing optional
  acceleration packages are not installation requirements for the base app.
- The desktop UI and bundled-resource lifecycle include the merged integration
  repairs from Host PRs 254–256; resource installation does not grant new Host permissions.

## Acceptance and publication boundary

The earlier store integration qualified source and actual packaged desktop flows,
including six local Git/HTTPS publications, withdrawal, block/unblock, application
restarts and settings/backup NUL recovery. Those results are evidence for their
recorded code identity, not the final Alpha 1 candidate. The Alpha 1 acceptance
record must bind its exact head, full source archive, resource SHA and actual
packaged resources before publication.

The final release gates are:

1. Full clean-source archive generation and identity verification; build from its
   extraction, verify all bundled members, and exercise the actual native Japanese
   frontend from both extracted sources and Electron resources.
2. Fresh/default selection, older independent installation, update, rollback and
   personal-override preservation; desktop/store smoke on the packaged extraction.
3. Fresh exact-head CI and review. Passing a native pronunciation check or producing
   audio does not establish human listening or pitch-accent acceptance.
4. Explicit maintainer acceptance of the full resource's unverified-but-enabled
   admission policy and the remaining linguistic/listening limits documented in
   [the dictionary guide](japanese_pronunciation_dictionary.md). Code/build success
   does not resolve that product decision.
5. Explicit publication approval, immutable new-version assets and checksums,
   plus the pinned HTTPS resource URL for the full tag/manual workflow. The candidate
   pin nominates the Alpha 1 Release asset URL; it is not live while unpublished.
   Upload matching assets to the draft Release before publication, then publish the
   new tag/Release so the full workflow can fetch those exact bytes. This proposed
   URL is not approval to upload/publish data, and these notes create no tag or Release.

Retain the rc2 designation and all reviewed/unverified distinctions. Do not call
this an exhaustive pronunciation or voice-quality validation. Follow the
[full application distribution procedure](../release/README.md#6-bundle-the-default-dictionary-with-the-application).

## 中文说明

本轮是 #245 的发布候选更正，使用新的 `v0.17.1-alpha.1` 身份，不移动旧标签或替换旧版
产物。完整应用包随附固定摘要的 rc2 词典，独立安装版本与个人词典仍按原有优先级保留。
插件商店与 UI、依赖修复一起重新验收，Pi 致谢只是同一发布 PR 的一项。

构建与自动测试通过不等于全部词条已人工审核或试听。完整词典仍保留未逐词确认但启用的
数据，发布前必须明确接受这一策略和已记录的质量限制；本候选文档本身不构成发布批准。
