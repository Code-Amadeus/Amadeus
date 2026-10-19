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

## Local derivative and distribution boundary

`tools/prepare_japanese_dictionary.py` accepts only the pinned source contents
(after newline normalization), then applies the separately tracked
`tools/data/japanese_dictionary_corrections.csv`. The recipe records each
correction, the source revision, the source hash, and the source-chain limits.
Preparation additionally normalizes Unicode reading fields, untranslated
initialism readings and native mora counts without moving valid accent nuclei,
then removes only identical full rows. Different lexical readings remain.
The generated `provenance.json` additionally identifies the derivative's
version, hash, entry count, normalization rules and repair counters.

Version 2026.10.20.2 retains the previous correction history and adds a
source-backed `let's → レッツ` repair: the existing `lets` entry has the same
CMU phones. Its accent annotation is reused, not validated by listening.
The historical 317-row review comprises 94 lexical/script/annotation repairs,
65 representation repairs and 158 unconfirmed readings. The release filter
yields known whole words and redundant letter readings to the pinned base
and quarantines every variant of CMU final-phone risk surfaces. CMU/base
hashes, rules and complete exclusion records are in provenance. The direct
flags reproduce the external 1,557-row TSV; quarantine of their other variants
excludes another 112 rows. This screen admits false positives and can miss
other errors; it does not establish that each excluded row is wrong.

The prepared CSV and frontend share full-token boundaries on retained Latin
entries. Only complete registered official tokens are marked, preventing
prefix and suffix intrusion while leaving unlisted/base/legacy/personal-only
inputs unchanged. All original source costs are preserved; historical cost
calibration and the common floor are retired. This CSV requires the matching
frontend integration, not only an OpenJTalk user-dictionary registration.

From 127,808 normalized entries, 2,502 are excluded (849 base-owned rows,
1,669 CMU-risk rows, 16 overlap), leaving 125,306. Three-context validation
covers all 814 base-owned surfaces, and 290 common English words extend the
former 52-control sample. Native selection across 122,978 retained distinct
surfaces in three contexts loses no previously selected whole-word match.
These checks do not establish exhaustive lexical accuracy. `proposer` still
uses a provisional flat accent; waveform/listening acceptance is outstanding.

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
