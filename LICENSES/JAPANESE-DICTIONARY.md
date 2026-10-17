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

Version 2026.10.16.3 retains the previous 11 singular-reading repairs and
complete `proposer` entry, and reviews the remaining 317 flagged entries:
94 lexical/script/annotation repairs, 65 phonetic representation repairs and
158 unchanged entries awaiting stronger evidence. The recipe identifies each
reviewed before/after pronunciation, relevant CMU/native evidence and measured
word-cost adjustments. Other readings of a corrected surface remain available.
Only repaired entries receive native MeCab surface spelling. `proposer` still
uses a provisional flat accent;
native listening review is still required. This does not claim exhaustive
dictionary accuracy or contextual disambiguation of all Japanese homographs.

The CSV and notices are distributed separately as the optional
`japanese-pronunciation` asset pack. Large generated pack files remain outside
Git/source archives. No compiled `user.dict`, local `userdict.md5`, personal
CSV, model weights, or recordings are included in this pack. Compilation uses
the installed pyopenjtalk-plus runtime and writes only disposable cache files.
Updates do not overwrite the optional personal `assets/text/ja/user.csv` or
the existing legacy directory.
