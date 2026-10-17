# Optional Japanese pronunciation dictionary / 可选日语发音词典

`japanese-pronunciation` adds English loanword and selected name readings to
local GPT-SoVITS Japanese synthesis. It is independent of a character, voice
checkpoint and emotion-reference pack. No automatic download is performed;
the base Japanese frontend remains usable without it.

`japanese-pronunciation` 为本地 GPT-SoVITS 日语语音补充英文单词及部分人名读音。
它独立于角色、模型权重和情绪参考包；不会自动下载，未安装时仍可使用基础日语前端。

## Install and control / 安装与控制

Update the application to a version containing this pack's catalog entry and
loader before installation. Older applications reject the unknown pack ID.
The first compatible application release has not yet been published.

```powershell
python tools/external_assets.py verify output/amadeus-japanese-pronunciation-2026.10.18.1.zip
python tools/external_assets.py install output/amadeus-japanese-pronunciation-2026.10.18.1.zip
python tools/external_assets.py status
```

Restart the backend after installation, updates, or switching the Voice setting
**Use installed Japanese dictionaries**. Its environment key is
`ENABLE_JAPANESE_USER_DICTIONARY`; `false` disables all optional/personal/legacy
user dictionaries, leaving the base dictionary active. It defaults to true to
preserve existing installations, but does not install anything.

安装前须更新到包含此词典目录及加载器的应用版本；旧版会拒绝未知包。
安装、升级或更改“语音 → 使用已安装的日语词典”后需重启后端。
`ENABLE_JAPANESE_USER_DICTIONARY=false` 使用基础词典。
默认允许使用已安装词典，以保留现有本机行为；不代表自动安装资源。

Resource status reports the pack version and validates the CSV shape,
provenance hash/count and required notices without importing model libraries.
The Japanese frontend logs actual compilation/loading failures and keeps the
base frontend usable. The runtime ready state is separate from pack presence.
Missing optional data is a normal, inactive state. The card is active only for
Japanese GPT-SoVITS output with a present or loaded source. For subprocess
synthesis, actual native state and identity travel in the existing ready/done
messages; installation status is not evidence of native activation.

## Data and personal files / 数据与个人词条

- Official data installs below `assets/text/ja/english/`.
- Personal CSV entries may live at `assets/text/ja/user.csv`, outside the pack.
  Use OpenJTalk's 15-column CSV format and appropriate entry costs; dictionary
  registration order alone does not promise precedence for duplicate words.
  Save as UTF-8 **without BOM**: native MeCab would otherwise treat the BOM as
  part of the first surface. Empty, malformed or uncompileable sources are
  individually reported with their relative filename and skipped. A broken
  personal CSV does not disable a valid official pack.
- If the new pack is absent, the existing
  `GPT_SoVITS/text/ja_userdic/userdict.csv` remains a supported local source.
  An installed pack replaces that legacy source's role; an invalid installed
  pack is reported instead of silently switching back to the legacy copy.
- Compilation writes only `.cache/japanese_dictionary/`, keyed by source bytes,
  native runtime version and the installed system dictionary identity. The
  application does not rewrite or distribute personal/legacy CSVs or binaries.
  A peer-published binary for the same content key is reused if Windows locks
  replacement. Successful loading prunes obsolete managed binaries; files still
  mapped by another live process are retried at a later successful startup.
- First-sentence audio cache keys identify the native sources **actually loaded**
  in the synthesis process, including its native runtime/system dictionary.
  A base fallback has a different identity from a loaded pack. Unknown native
  state bypasses both cache reads and writes. Japanese pre-identity legacy
  entries are not migrated; the existing English migration remains supported.
  Japanese frontend setup precedes backend ready; later lazy language loads
  refresh the sidecar's identity at inference completion.
- The optional loader reports unsupported APIs without blocking base frontend
  import. User dictionaries require the pinned `pyopenjtalk-plus` API; an older
  `TTS_PYTHON` environment is not silently adapted or claimed fully supported.

用户 CSV 与正式包分开，安装和升级不会覆盖。旧词典目录也不会被删除或改写。
编译结果是本机缓存，不能作为可维护的词典源。个人重复词条的优先级须通过
OpenJTalk 词条代价控制；文件排列顺序不保证覆盖。

## Prepare a local release candidate / 准备本地候选包

The checked recipe and license evidence live in
`tools/data/japanese_dictionary_source.json` and
`LICENSES/JAPANESE-DICTIONARY.md`. Obtain that exact upstream CSV separately,
or use an existing matching local copy. Preparation is offline, normalizes LF,
checks the source hash and applies the tracked correction CSV. The maintainer's
preparation environment needs the existing local-model dependency
`pyopenjtalk-plus==0.4.1.post9`, pinned in the recipe for repeatable mora counts.
Installation/status checks need no model libraries. Preparation refuses a
different customized source and differing existing output unless `--overwrite`
is explicit.

```powershell
python tools/prepare_japanese_dictionary.py --source GPT_SoVITS/text/ja_userdic/userdict.csv
python tools/validate_japanese_dictionary.py --output output/japanese_dictionary_validation.json
python tools/external_assets.py build japanese-pronunciation --output output/amadeus-japanese-pronunciation-2026.10.18.1.zip
python tools/external_assets.py verify output/amadeus-japanese-pronunciation-2026.10.18.1.zip
python tools/audit_japanese_dictionary.py --source assets/text/ja/english/userdict.csv --output output/japanese_dictionary_audit.json
```

The bundle contains the prepared CSV, its version/provenance and complete
license notices. It excludes compiled dictionaries and extra working files.
Preparation and these commands do not upload, publish, alter `.env`, change
model paths or rebuild any existing model/voice archive.

## Quality boundary / 质量范围

Version 2026.10.18.1 retains the review of the 317 entries left by version 2026.10.16.2:
94 receive reviewed lexical/script/annotation repairs, 65 receive phonetic
separator or format-control repairs, and 158 retain their readings, costs and
accent fields pending stronger evidence. Of these, 48 receive surface spelling
normalization only. The local review report records every
disposition. An all-kana alternative alone is not proof of lexical correctness.
Examples include `balloonist` (`二` → `ニ`), `ninth` (`ん` → `ン`), untranslated
letter names, stray inflection letters and literal LLM explanations.

The correction recipe targets the reviewed **before pronunciation**; it does
not replace every pronunciation sharing that word. Legitimate alternatives
such as singular/plural `corps` and initialism/plural `CDS/CDs` remain available;
no context classifier is added to force a single choice.

Native input converts apostrophes and hyphens to `’` and `−`. All entries receive
this native surface spelling, including previously dormant contractions and
compounds; this changes representation rather than guessing readings. The frontend preserves
Latin compound punctuation until native tokenization. Standalone punctuation
and Japanese punctuation keep their normal handling. Native MeCab measurements
in three synthetic contexts exposed cheap unintended word splits; 109 reviewed
entry costs were reduced just enough to beat those measured splits. The
measured costs are stored in the CSV/recipe, with no runtime word-specific
pronunciation or cost rules. Unconfirmed entries retain their original rows.

One- and two-letter Latin surfaces are excluded at build time: these fragments
must yield to the base Japanese dictionary's letter and measurement entries.
This excludes 328 pre-deduplication rows (300 distinct previous pack rows),
preventing imported `m`/`w`/`h` fragments from displacing cm/kg/km/mm/ml/kW.
No per-unit or per-word runtime list is added. Unknown short number suffixes
also bypass generic initialism expansion; known names and longer initialisms
such as `3GPUs` retain their existing handling. Native spelling is normalized for
8,998 pre-deduplication surfaces.

The pack contains 127,811 entries. Re-auditing it produces no further structural
repairs and leaves the 158 review entries intact. No accent is clamped or
invented for `guangjo`, whose source accent exceeds the native mora count.

The earlier version's 11 singular-reading repairs are retained:
`follower`, `learner`, `abstraction`, `acronym`, `adapter`, `airline`,
`applicator`, `bank`, `component`, `developer`, and `handler`. Their plural
entries remain intact. It also adds the complete `proposer` → `プロポーザー`
entry; its flat accent is provisional pending listening review.
The `component` row also has its entry cost corrected from 608 to 0: native
full-pack validation otherwise selects `comp + on + ent`, despite the correct
whole-word reading being present. The corresponding `components` cost changes
from 659 to 0 to keep its existing plural reading selected. Both sides are
checked by the native test. These repairs stay in dictionary data.

Build-time normalization uses general rules: NFC in reading fields,
removing phonetic-neutral separators only from otherwise pure kana when the
existing accent remains valid, spelling untranslated whole-token initialisms,
counting morae from one explicit
native pronunciation while preserving valid accent nuclei, and removing only
identical full rows. It does not strip English plural suffixes automatically
or merge different readings of one word. Passing kana through ordinary g2p
would retokenize it and can incorrectly split small kana; the audit feeds the
pronunciation directly to the native label builder instead.

Preparation records repair counters before deduplication; the release's 65
representation repairs are explicitly tracked alongside their measured costs.
The audit also lists CMUdict singular/inflection collisions as review candidates;
such collisions alone do not prove errors (`path` and `paths` can both be
`パス`). It does not guess replacements for these entries.

运行时没有新增逐词判断。原词条修复记录在 correction CSV；通用数据清理在构建时
执行，来源、修复理由、规则及计数均随包保存。不同读音保留，无法确定的重音或
机器翻译残留只生成待核对清单。后续可沿用同一审计流程逐批修复原数据。

Whole-token abbreviation preparation handles `MLP` next to Japanese, fullwidth
Latin text and case variants of existing names, and avoids replacing `API`
inside `CAPITAL`. Known names/initialisms followed by plural or possessive
suffixes convert only their stem and preserve the suffix. It applies to Japanese synthesis text; English output,
subtitles and conversation history retain their original text.

The upstream translation is not exhaustively human-verified. This is an English
word/name extension, not a general context-sensitive Japanese homograph model
or a voice/timbre replacement. Native pronunciation/prosody comparisons do not
replace end-to-end synthesis and human listening acceptance.

The native release gate compares 52 samples, including Latin units and
inflected names/initialisms, against the actual base `japanese.g2p` prosody path.
It also checks 172 reviewed rows and nine common contractions in three contexts
(543 native contexts) and 181 production frontend/native-prosody comparisons.
An additional 32-unit set has no phoneme changes. It records an `rpm` prosody
boundary difference for listening review: the reading is the same, and that
observation alone does not justify changing an existing accent.
It runs offline with a prepared full pack; core unit tests use synthetic data
and do not require downloading dictionary or model assets. Pack validation
checks the hash before parsing CSV and memoizes successes/failures by member
file stats. Audio cache key generation performs no dictionary-file validation.

## Publication / 发布

Keep each ZIP version immutable and attach it, with a separately published
SHA-256, to a GitHub Release in the existing resource repository or resource
Release, if it still allows new attachments. A published immutable Release
cannot accept new assets; use a new dictionary-only resource Release in that
case. An organization resource page can link to that exact asset and explain
the minimum compatible application release, changes and known limitations.
Adding this independent ZIP does not require rebuilding earlier model, voice,
visual or emotion packages. The application code itself needs a compatible
application release. A later correction gets a new dictionary version/filename;
do not replace an older same-named asset.

可编辑的原资源 Release 可以追加独立词典 ZIP；不可变 Release 需新建仅含词典的
资源 Release。旧资源包不必重打包。组织资源页面作为
介绍与下载入口，并标明所需应用版本。词典升级新增版本文件，保留旧文件。
