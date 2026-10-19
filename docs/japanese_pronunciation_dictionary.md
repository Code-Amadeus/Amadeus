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
python tools/external_assets.py verify output/amadeus-japanese-pronunciation-2026.10.20.2.zip
python tools/external_assets.py install output/amadeus-japanese-pronunciation-2026.10.20.2.zip
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
python tools/external_assets.py build japanese-pronunciation --output output/amadeus-japanese-pronunciation-2026.10.20.2.zip
python tools/external_assets.py verify output/amadeus-japanese-pronunciation-2026.10.20.2.zip
python tools/audit_japanese_dictionary.py --source assets/text/ja/english/userdict.csv --output output/japanese_dictionary_audit.json
```

The bundle contains the prepared CSV, its version/provenance and complete
license notices. It excludes compiled dictionaries and extra working files.
Preparation and these commands do not upload, publish, alter `.env`, change
model paths or rebuild any existing model/voice archive.

## Quality boundary / 质量范围

Japanese kanji readings that depend on context are a separate optional capability;
see [Japanese contextual readings](japanese_context_reading.md). Its model and
switch are independent of this English loanword pack. Disabling this dictionary
does not disable an installed context-reading model.

Version 2026.10.20.2 retains the historical review of 317 entries: 94
lexical/script/annotation repairs, 65 separator/format repairs, and 158
unconfirmed readings. Those decisions are evidence, not mandatory release
inclusions. The correction CSV has 172 rows after retiring the cost-only
`components` entry and adding `let's → レッツ`. The latter reuses the existing
`lets` reading and accent annotation: both have CMU `L EH1 T S`.

Preparation applies these rules offline, after representation repairs:

- Yield every surface recognized as one known whole word by an independent
  base MeCab instance to that base. Also remove redundant letter/initialism
  readings identical to the base result, such as `rpm`. Other unknown words
  remain eligible for improved readings. Pin the runtime and base dictionary.
- Quarantine every variant of a surface if any reading's final Katakana
  consonant conflicts with every CMU pronunciation variant: ズ requires S/Z,
  ド requires D/T, and ング requires NG. Removing only one bad variant could
  promote another shifted reading (`directly → director`).
- Surround retained Latin surfaces with native fullwidth spaces. The frontend
  applies both boundaries only to complete tokens present in the successfully
  registered official CSV, after sentence/punctuation splitting. Thus `mode`
  cannot intrude into `model`, nor `con` into `icon`. Unlisted words and units
  keep their input unchanged; base, legacy and personal-only behavior is kept.
  Both prosody and plain-phoneme paths use this contract. Raw pyopenjtalk callers
  of this prepared CSV must use the same helper. This requires the matching
  frontend code, in addition to the installer/catalog entry.

All original source costs are preserved. Whole-token boundaries supersede
both the historical 109-entry calibration and the rejected common cost floor;
changing costs traded correct short-word readings against longer-word splits.
Historical cost evidence is explicitly marked retired. There are no runtime
per-word reading exceptions; the runtime vocabulary comes from the exact
successfully loaded official bytes and is cleared on disable/load failure.
The audio-cache identity includes this frontend contract.

From 127,808 normalized rows, the policy excludes 2,502 rows: 849 base-owned
rows and 1,669 rows associated with CMU-risk surfaces (16 overlap). The resulting
pack has 125,306 rows; 125,303 Latin rows receive word boundaries. Provenance
records every exclusion's normalized input line, surface, reading and reason.
CMU data and native base hashes are pinned.

The direct flags exactly reproduce all 1,557 external TSV records (ズ 807,
ド 523, ング 227); none of those original word/reading pairs remain. Another
112 same-surface variants are quarantined to avoid promoting shifted rows.
These are conservative exclusions, not 1,669 proven errors: `with → ウィズ`
and `condo → コンド` can be legitimate loanword adaptations. The rule can miss
interior shifts and vowel-ending errors. The older singular/inflection collision
list remains review-only (`path` and `paths` can both be `パス`).

构建时优先使用基础词典，隔离词尾音可疑的整个词形；正式包只在完整词边界命中，
避免短词串入其他单词。原有词条代价全部保留，代价补丁已撤掉。未收录的词、
单位及仅使用个人或旧词典的路径保持原输入。隔离清单含误报，不等于全部确认有错；
可确认的 `let's` 错行使用已有 `lets` 的读音修复。

Whole-token abbreviation preparation handles `MLP` next to Japanese, fullwidth
Latin text and case variants of existing names, and avoids replacing `API`
inside `CAPITAL`. Known names/initialisms followed by plural or possessive
suffixes convert only their stem and preserve the suffix. It applies to Japanese synthesis text; English output,
subtitles and conversation history retain their original text.

The upstream translation is not exhaustively human-verified. This is an English
word/name extension, not a general context-sensitive Japanese homograph model
or a voice/timbre replacement. Native pronunciation/prosody comparisons do not
replace end-to-end synthesis and human listening acceptance.

The native release gate compares the original 52 controls and 32 additional
unit samples against the production `japanese.g2p` path. It also compares all
814 base-owned imported words in three contexts (2,442 controls), exercises
290 frozen common English words, including all 48 distinct words in the
external 49-row frequent-document subset, and validates the 11 reported regressions
with reviewed readings and five unchanged base fallbacks. The broader
vocabulary is selected from frequent tracked Markdown words and flagged
surfaces; inclusion alone is not a pronunciation correctness oracle. Changes
for other unknown words are recorded for review.

Retained corrected rows and contractions pass 516 native contexts and 172
production frontend/prosody comparisons. There are no failures or unit-prosody
changes in this candidate. These results apply to these controls and the
pinned native environment; they do not establish zero regressions across all
English vocabulary or replace waveform/listening acceptance.

Across all 122,978 retained distinct surfaces in three contexts, the new native
selection preserves every complete-word match from the previous pack and
selects a complete word for previously split inputs. This checks selection,
not the correctness of every imported reading.

The gate runs offline with a prepared full pack. Core tests use synthetic data
and require no dictionary/model downloads. Pack validation checks hash before
parsing CSV and memoizes success/failure by member stats. Audio cache keys
perform no dictionary-file validation.

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
