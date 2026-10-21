# Independently versioned Japanese pronunciation resources

The dictionary is an optional resource, versioned independently of the
application. Mainline owns loading, validation, matching boundaries, cache
identity and build/test tools. The complete source, corrections and review
history belong to the separate data workspace. Generated CSVs, resource ZIPs,
compiled dictionaries and audio do not belong in the application source tree.

The format-2 candidate preserves **134,201 original rows**. Its first snapshot
contains **462 source-reviewed reading corrections** and **133,739 unchanged
original rows labelled `original_unverified`**. Preservation is not a claim that
all original pronunciations are correct. The accumulated supplement has 4,534
rows / 4,532 words and is combined with the full table at runtime; it is not a
small replacement deliverable. See [acceptance evidence](evidence/japanese_resource_acceptance_2026-10-21.json).

Release 0.17.1 was withdrawn. The current package is a local release candidate
for independent review; neither this document nor successful validation
publishes it. Further corrections can ship as new resource versions without a
new application release when the data contract is unchanged.

## Data contract

A format-2 pack contains nine fixed members below `assets/text/ja/english/`:

- `source.csv`: complete, immutable upstream snapshot, checked against its
  recorded source revision, content hash and row count.
- `userdict.csv`: complete reconstructed master, in source-row order. Every
  row either equals its original or has an exact reviewed before/after record.
- `overlay.csv`: accumulated explicitly reviewed readings and compound
  constraints, with a declared native-word/alias partition.
- `provenance.json`: independent resource version, format version, hashes,
  source identity, corrections, runtime dispositions and separate accent status.
- `NOTICE.md`, `GPT-SoVITS-MIT.txt`, `CMUdict-BSD-2-Clause.txt`,
  `Amadeus-AGPL-3.0.txt`, and `OpenJTalk-COPYING.txt`: retained source notices.

The loader rejects unrecorded edits, missing source rows, stale correction
pairs, changed source surfaces/costs, inconsistent overlay readings and aliases
that collapse distinct retained readings. Unchanged rows do not enter a fake
"all reviewed" ledger. A pending accent cannot enter the native CSV selection:
its reading must use the reviewed kana path or defer to the base frontend.

Runtime exclusions are explicit: base ownership, short tokens, contextual
ambiguity or pending accent. Excluded rows remain in both the source/master
history. The runtime view uses whole Latin tokens; partial matches such as
`mode` inside `model` or a name inside an identifier cannot introduce aliases.
Native lexical ownership is checked before optional case folding: `API` and
`Disney` keep their known base readings, while unrecognized optional spellings
can still use their registered reading. This uses the installed base lexicon,
not a hardcoded exception list or another model pass.

The base dictionary hashes are pinned because morphology and ownership depend
on them. A mismatched base fails visibly; it does not silently activate another
full dictionary. Uncovered words follow native GSV. Context-dependent Kanji
readings are not forced into one global mapping. Context-reading model weights
remain a separate default-off experiment and are not included in this pack.

## Build from the independent data workspace

The data workspace has its own Git history and contains `source.csv`,
`review.json`, `overlay.csv` and source notices. `review.json` is the editable
source of exact corrections and deferred words; generated `userdict.csv` is
not hand edited. Original CSV bytes are pinned with Git attributes so a
checkout on another platform cannot silently change their hashes.

```powershell
python tools/prepare_japanese_full_resource.py --data-dir <data-checkout> --output-root <fresh-output-root> --version <new-resource-version>
```

The builder validates the pinned source, reconstructs the full master, reuses
the existing native/alias paths, computes explicit ownership dispositions and
validates the finished pack. Use the project's pinned native runtime. It does
not download, install or publish anything. The ordinary bundle API then builds
and verifies the prepared output:

```python
from pathlib import Path
from tools.external_assets import build_bundle, verify_bundle

index = Path("assets/index.json")
archive = Path("output/amadeus-japanese-pronunciation-<version>.zip")
build_bundle(project_root=Path("<fresh-output-root>"), output=archive,
             pack_ids=["japanese-pronunciation"], index_path=index)
verify_bundle(archive, index_path=index)
```

Keep each published ZIP immutable. Store its SHA-256, exact data Git revision,
compatible application contract, correction list and known limitations with
that release. Publish the corresponding data-source archive separately; keep
voice recordings, personal dictionaries, environment files and generated
native binaries out of both archives. No incremental-patch downloader or
background update service is required for the first version.

## Install, update and rollback

Use an application containing the format-2 loader. Earlier applications reject
this pack; they cannot be made compatible by relabelling its rows as reviewed.
Format-1 checked subsets remain loadable for existing installations and rollback.

```powershell
python tools/external_assets.py verify <dictionary-version.zip>
python tools/external_assets.py install <dictionary-version.zip>
python tools/external_assets.py status
```

For an intentional update, use `install <new-version.zip> --overwrite`.
For rollback, use `install <previous-version.zip> --overwrite`.
Restart the backend after either operation. The installer verifies all members
before writing, rejects unintended conflicting files, restores replaced files
if installation fails, and skips an identical reinstall. It never modifies
`assets/text/ja/user.csv` or the legacy source directory.

Only one official resource version is active. The installed official pack takes
precedence over the legacy CSV; personal entries remain a separate explicit
input. Disabling **Use installed Japanese dictionaries** disables optional,
legacy and personal dictionaries. An invalid installed official pack is reported
instead of silently switching back to the legacy CSV. Native compilation writes
only disposable `.cache/japanese_dictionary/` files.

The audio identity includes the resource metadata and native runtime identity.
Updating readings, alias/native mode or runtime dispositions changes it;
rolling back restores the previous identity. This prevents reuse of speech
cached under different dictionary behavior.

## Acceptance and later maintenance

A first release does not require an impossible all-rows correctness claim.
It requires complete source accounting, source-backed changes, bounded
matching, explicit unresolved cases, installation/update/rollback checks,
regression controls and representative actual GSV listening.

The current sample uses complete original sentences, not masked/truncated
fragments: 672 stored synthetic replies and 5,000 JSUT sentences. Changed phone
sequences are retained as observations, not counted as verified improvements.
Four actual paired GSV cases cover menu, game, Disney and 中立派. They share
model, sampling parameters and reference conditioning. The maintainer reported
the candidate better in all four; that feedback is limited to those examples.
It is not exhaustive pitch-accent or whole-vocabulary certification.

All 451 corrected spellings were also exercised in three text positions:
1,342 of 1,353 contexts selected the recorded correction, with no previously
matching context lost. Eight contexts selected a different retained original
reading; those alternate source rows remain unverified, not silently removed
or certified. Three `accompaniments` observations could not be scored by the
whole-node matcher because native postprocessing writes the kana ニ as 二;
the observed phone sequence still contains `n i`. This is an application check,
not an independent pronunciation-accuracy estimate. The unresolved spellings
and observations are retained in the acceptance evidence for later review.

The first full native run exposed `API` being folded to an unrelated imported
`api`. The owning boundary now preserves exact native spellings; all 83 base
controls retain their input phones. Source-unverified rows and unresolved
prosody remain visible. The resource stays unpublished pending independent PR
review and final maintainer release acceptance.

For a later reported error, preserve the original sentence and resource version,
verify the reading and scope, add an exact correction and a regression case,
then build a new complete resource version. Ambiguous evidence stays pending;
no missing reference is a reason to silently delete coverage.
