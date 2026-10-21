# Optional Japanese pronunciation dictionary

Checked on 2026-10-16 against the canonical `RVC-Boss/GPT-SoVITS` repository
at commit `1b47ca07ef43a284e0b8bd7f29d3e1e3ff76082a`:

- Source: `GPT_SoVITS/text/ja_userdic/userdict.csv`.
- Git blob: `2be580b461185ce18c9000d9ec6c8032623e17fd`.
- SHA-256 of the UTF-8/LF file:
  `d857e443ee48d9641096816a98996669602895411e4330d7d91d1dbe1103389f`.
- Source size: 17,180,971 bytes, 134,201 entries.
- The existing local CSV matches this source after CRLF-to-LF normalization.
  This comparison identifies its contents, not the original download event.
- Last upstream CSV change: `eee607b71dae07fb3b29f8f2727490039b3ac984`.

Source permalink:
https://github.com/RVC-Boss/GPT-SoVITS/blob/1b47ca07ef43a284e0b8bd7f29d3e1e3ff76082a/GPT_SoVITS/text/ja_userdic/userdict.csv

## Attribution and license basis

The immediate upstream repository carries the MIT license, copyright 2024
RVC-Boss. Preserve its complete notice as `GPT-SoVITS-MIT.txt`.

Upstream PR https://github.com/RVC-Boss/GPT-SoVITS/pull/1660 describes a
CMUdict-to-Katakana conversion using ChatGPT and DeepSeek. PR
https://github.com/RVC-Boss/GPT-SoVITS/pull/1670 restores that dictionary
after fixing imports. The author explicitly identifies imperfect translation
accuracy. The exact original CMUdict revision and per-entry verification are
not recorded in those PRs. Preserve `CMUdict-BSD-2-Clause.txt` for this stated
source chain; its existing 0.07 notice does not establish the exact translation
input revision. This release relies on the immediate upstream MIT grant,
consistent with the project's recorded upstream reliance policy, rather than
inventing a separate license for generated translations.

Amadeus's preparation code and corrections follow the project's root
AGPL-3.0 license. The pack includes that complete text as
`Amadeus-AGPL-3.0.txt`, in addition to the upstream notices. Do not remove
upstream attribution or relabel the upstream work as solely Amadeus-authored.

## Full-source format 2

The independently versioned format-2 candidate keeps the complete original
134,201-row source and a complete reconstructed master. Exact before/after
records identify reviewed reading corrections; unchanged original rows remain
explicitly unverified. Runtime deferrals do not delete their source records.
The older admission discussion below applies to historical format-1 subsets,
which remain supported for existing installations and rollback.

The accumulated reviewed supplement and later corrections also use source
material from UniDic 2025.12 and Alkana, and native lexical features from the
pinned OpenJTalk dictionary. The independent data workspace records the source
identities and evidence. Its resource NOTICE includes the UniDic BSD and Alkana
GPL-2.0 notices and original attribution; OpenJTalk's complete COPYING is a
separate required member. These sources are not relabelled as solely Amadeus
work. Resource/data-source archives are prepared separately from application
source; neither archive is published by the build tools.

The Kanji compound readings also use JMdict, copyright Electronic Dictionary
Research and Development Group, under CC BY-SA 4.0. The resource NOTICE retains
attribution, source and license links, and the description of selected/modified
readings. Source: https://www.edrdg.org/jmdict/j_jmdict.html ; license:
https://creativecommons.org/licenses/by-sa/4.0/ .

Alkana comparison readings are research corroboration only and remain in the
independent data repository. The runtime provenance omits `alkana_reading`
values; it does not need those comparisons to load or select any entry.

Current candidate validation and publication limitations are recorded in
`docs/evidence/japanese_resource_personal_followup_2026-10-22.json`. A checked hash or
successful build establishes data identity and structure, not complete lexical,
accent, perceptual, or redistribution acceptance.

## Historical format-1 derivative and distribution boundary

`tools/prepare_japanese_dictionary.py` accepts only the pinned source contents
(after newline normalization), then applies the separately tracked
`tools/data/japanese_dictionary_corrections.csv`. The recipe records each
correction, the source revision, the source hash, and the source-chain limits.
Preparation additionally normalizes Unicode reading fields, untranslated
initialism readings and native mora counts without moving valid accent nuclei,
then removes only identical full rows. Different lexical readings remain.
The generated `provenance.json` additionally identifies the derivative's
version, hash, entry count, normalization rules and repair counters.

Version 2026.10.20.3 admits only exact word/reading pairs in the hash-pinned
`tools/data/japanese_dictionary_reviewed_readings.json`, after base ownership
and CMU quarantine. The smaller candidate has 129 rows / 127 distinct words.
The ledger is an individual model-assisted lexical candidate check, not
independent human/native-speaker or listening sign-off. Unreviewed source rows
and historical corrections are research evidence, not release approval.

The historical 317-row review comprises 94 lexical/script/annotation repairs,
65 representation repairs and 158 unconfirmed readings. Those unconfirmed
rows remain in research inputs; release admission now requires the separate
checked ledger. The source-backed `let's → レッツ` repair reuses the existing
`lets` annotation and CMU phones. `proposer` has a provisional flat accent.
Neither native compilation nor these lexical checks establish accent quality.

Base/CMU hashes, ledger identity, accepted readings, exclusion counts and an
excluded-row-set hash are in provenance; rejected readings are not shipped in
that metadata. Source, correction and ledger hashes reproduce the selection.
The CMU screen reproduces the 1,557 direct flags plus 112 variants, including
false positives. It can miss interior/vowel shifts and is not a correctness
oracle. Every shipped pair must pass ledger membership independently of it.

The prepared CSV and frontend share full-token boundaries on retained Latin
entries. Only complete registered official tokens are marked. Original costs
are preserved; there are no runtime per-word reading exceptions. This CSV
requires the matching frontend integration, not just native registration.
Old full-data candidates without a checked ledger are retired and rejected.

The production gate checks every retained word in three contexts (381), all
129 row memberships, 303 common English controls (190 unlisted fallbacks), 52
baseline controls, 32 extra units and 814 base-owned words in three contexts.
These checks do not establish exhaustive lexical accuracy. Independent review
and human listening acceptance remain outstanding; this candidate is local.

The upstream default branch was rechecked on 2026-10-20 at
`b4570b876f5c086819ba84e3215472733c6a7b70`: the CSV still has the same Git blob
and contents as the pinned source above.

The CSV and notices are distributed separately as the optional
`japanese-pronunciation` asset pack. Large generated pack files remain outside
Git/source archives. No compiled `user.dict`, local `userdict.md5`, personal
CSV, model weights, or recordings are included in this pack. Compilation uses
the installed pyopenjtalk-plus runtime and writes only disposable cache files.
Updates do not overwrite the optional personal `assets/text/ja/user.csv` or
the existing legacy directory.
