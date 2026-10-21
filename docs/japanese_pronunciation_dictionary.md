# Independently versioned Japanese pronunciation resources

The optional dictionary is versioned independently of the application. Mainline
owns loading, validation, matching, cache identity and build/test tools. Full
source, corrections and research history belong in a separate data Git repository.
Generated CSVs, ZIPs, compiled dictionaries and recordings are not application code.

The current candidate preserves **all 134,201 original rows**: 462 reviewed
corrections and 133,739 unchanged rows labelled `original_unverified`. The
supplement contains 4,539 rows / 4,537 words; it supplements the full table.
Preservation and automated screening do not establish pronunciation accuracy.

Release 0.17.1 was withdrawn. Candidate **2026.10.22.1-rc2** remains unpublished
pending independent review and maintainer acceptance. See the
[current follow-up](evidence/japanese_resource_personal_case_2026-10-22.json).
The [2026-10-21 receipt](evidence/japanese_resource_acceptance_2026-10-21.json)
is historical and superseded.

## Source preservation and runtime admission

A format-2 pack has nine members under `assets/text/ja/english/`:

- `source.csv`: immutable upstream bytes, revision, hash and count.
- `userdict.csv`: reconstructed full master in source order. Each row is original
  or has an exact reviewed before/after record.
- `overlay.csv`: reviewed readings and compound constraints, with a native/alias
  partition and accepted-reading ledger.
- `provenance.json`: version, hashes, corrections, exclusions and separate accent
  status. Alkana comparison readings remain in research data, outside runtime metadata.
- `NOTICE.md`, `GPT-SoVITS-MIT.txt`, `CMUdict-BSD-2-Clause.txt`,
  `Amadeus-AGPL-3.0.txt`, `OpenJTalk-COPYING.txt`: retained source notices.

The builder reuses the existing CMU final-phone risk screen, defers known rejected
readings, and admits differing source readings only when reviewed. Identical MeCab
features do not establish contextual disambiguation. Both individually approved
variants of `when` and `without` remain supported. Contextual words are deferred;
pending accent uses an approved kana alias or stays out of native compilation.
Unsupported pronunciation characters are pending normalization/review, not deleted.

There are 7,989 source-row exclusions: 4,920 unresolved alternatives, 1,300 CMU
flags, 1,038 base-owned rows, 399 needing pronunciation-format review, 236 short
rows, 77 contextual rows and 19 known-rejected rows. Overlay replacement is a
separate step. Runtime contains **121,912 native rows and 4,411 aliases**, not that
many unique newly corrected words. Unchanged, single-reading, unflagged original
rows remain active and unverified. All excluded rows remain in source and master.

The loader rejects hidden edits, missing source rows, stale corrections,
inconsistent review ledgers and unresolved active alternatives. The screening is
heuristic; later errors can still require updates. Source preservation is not an
all-rows correctness claim.

## Matching and ownership

Exact base spellings keep native ownership; unknown spellings use the established
lowercase path. This applies without a pack, with format 1, format 2 and the legacy
CSV. Ordinary title-case words therefore retain the lowercase path, while native
`Disney` and `API` keep their exact readings. A reviewed alias may reinforce the
same base reading in context; it cannot replace a different native reading.

Aliases match complete tokens. Hyphens between letters retain compounds such as
`follow-up`; digits on either side form a boundary, so `Python-3` and `game-2` use
the word reading and established number path. Personal keys are removed from both
official aliases and native-word boundary insertion, allowing actual native output
to honor user corrections. Personal keys are collected before one official-set
subtraction, so adding personal entries does not copy the full official vocabulary
once per row. Personal surfaces are normalized with the native frontend in the
compiled snapshot, using the same case folding as text input before MeCab width
normalization. `Menu` therefore matches `menu`, while base-owned `Disney` keeps
its uppercase spelling. Source CSV bytes are never rewritten. Previously ineffective
halfwidth or title-case personal entries can start taking effect after upgrading;
this is an intentional compatibility change. The runtime identity changes so old
compiled/audio caches cannot mask the new behavior.

Uncovered words follow native GSV. Context-dependent Kanji are not forced into
one mapping. Context-reading model weights remain a separate default-off experiment.
Format 1 supports its published MeCab mode; the unused alias prototype is rejected.

## Reproducible build

The data Git repository contains `source.csv`, `review.json`, `overlay.csv`, pinned
`cmudict.rep` screening input and notices. Edit the review ledger, not generated
master rows. Git attributes preserve source CSV bytes; CMU is hashed after LF
normalization.

```powershell
python tools/prepare_japanese_full_resource.py --data-dir <data-checkout> --output-root <fresh-output-root> --version <new-resource-version>
```

Use the pinned native runtime. The builder reconstructs and validates without
installing or publishing. Package through `tools.external_assets.build_bundle`
and `verify_bundle`, pack ID `japanese-pronunciation`, index `assets/index.json`.
Keep published ZIPs immutable, with hashes, exact data revision, compatible loader
contract and known limits. Distribute a separate source archive, without personal
dictionaries, recordings or environment files.

Base hashes are pinned because ownership depends on the lexicon. Mismatch is
reported rather than silently enabling incompatible data. Hash and native-reading
contracts run in Windows, Linux and macOS model CI; consult the exact PR head's
check results for platform qualification.

## Install, update and rollback

Use an application with the format-2 loader. Published format-1 packs remain
supported for rollback. One official pack takes precedence over legacy data;
personal entries remain separate.

```powershell
python tools/external_assets.py verify <dictionary-version.zip>
python tools/external_assets.py install <dictionary-version.zip>
python tools/external_assets.py install <new-or-previous-version.zip> --overwrite
python tools/external_assets.py status
```

Restart after update or rollback. The installer verifies before writing, restores
replaced files on failure and skips identical reinstalls. Personal and legacy
files stay intact. Format-1 rollback selects the old pack even if unused format-2
files remain. An invalid official pack is reported without silent legacy fallback.
The setting disables all optional, personal and legacy inputs.

The audio-cache identity includes resource and native-runtime identities; rollback
restores the prior identity. A compiled format-2 cache skips normalization and CSV
serialization of the full native view, while validating sources. Startup still
costs seconds, substantially more than the small format-1 pack.

## Acceptance and later maintenance

At reviewed head `916fa472`, after #252 and #249 merged, seven inherited
Qwen/provenance conflicts were resolved to exact main versions. The Japanese
frontend and loader code had not changed in that integration. All seven profiles (26,552 records each) reproduce the prior
observations, and all nine resource members and the ZIP remain byte-identical.
The earlier [review receipt](evidence/japanese_resource_review_followup_2026-10-22.json)
remains available as history. Fresh final-head CI and independent review are
still separate acceptance steps; this integration does not authorize release.

The new production-path comparison covers 26,552 records against fixed **main
8de4c958**, under no-pack, published format-1, legacy CSV and candidate states.
The 12,027 repository fragments and 672 stored synthetic replies are regression
material, not random daily conversation. Another 5,000 JSUT sentences and targeted
case/reading controls are separate groups. Phone changes are observations, not
verified improvements or an error-rate estimate.

All 127 published readings match in their original three contexts (381/381).
Additional title-case `Main` keeps native `メーン` rather than ledger `メイン`,
recorded as a strict mismatch. Corrected spellings match in 1,350/1,353 contexts.
Three `accompaniments` spans are unscored because native postprocessing writes
kana ニ as 二; phones still contain `n i`. The overlay check has the same unscored
word. All 1,105 unowned-case controls retain main's phones. Of 83 unique base
controls, only `100kHzです` and `100GHzです` differ from main, using the unit path.

Four actual GSV A/B pairs (menu, game, Disney, 中立派) received maintainer preference
for the candidate. Final phone/prosody and reference sequences are unchanged;
model and reference hashes are checked. This reuses existing audio evidence,
not a new whole-vocabulary listening pass. Four preferences do not establish
absolute-reading or pitch-accent accuracy.

Actual format-1 → candidate → format-1 rollback → candidate restoration,
idempotence, personal/legacy preservation, failed-update restoration and
byte-identical independent reconstruction are covered. For later errors, retain
the sentence and resource version, add evidence-backed corrections and tests,
then build a new version. Uncertain evidence stays pending. Daily error-rate
improvement remains unquantified, and the candidate is not a published release.

### Personal-dictionary follow-up to the independent review

The independent review of `916fa472` reproduced the four earlier fixes. The
follow-up removes repeated full-set copying for personal overrides, normalizes
personal surfaces only in the compiled snapshot, and simplifies dead prefix
handling without changing the casing policy. The rc2 resource bytes and all
134,201 source rows remain unchanged. The current follow-up receipt records
fresh tests, corpus comparisons and Windows configuration measurements.
The subsequent review of `69368c19` confirmed those fixes. The current follow-up
also applies input case normalization to personal surfaces before compilation,
with regressions for official aliases, format-1/format-2 native words and a
case-owned base name. Fresh CI must qualify the new head separately from prior
17-green runs. Resource data and admission policy remain unchanged.

`Hand` remains a known contextual base-segmentation exception (`これはHandです`
can split into H + and). Blanket folding of equal-reading base spellings was
rejected after the full legacy CSV comparison exposed changes to other readings.
This follow-up does not claim to fix that exception or certify native acoustics.

Data review remains separate from code qualification. External review identified
`hidden`, `rollback`, `these`, `repair`, `subsequent`, `kerr` and `lumley` as
priority correction candidates, and `directory`, `between`, `below`, `characters`,
`did`, `rather` as priority deferred alternatives to review. These are pending
linguistic checks, not newly approved mappings; ambiguous names must not receive
one forced reading. The unverified-but-enabled admission policy (#244) still
requires explicit maintainer acceptance. Keep the PR draft and resource unpublished
until that decision and listening acceptance are complete.
