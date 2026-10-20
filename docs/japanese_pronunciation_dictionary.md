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
The first compatible application release is **v0.17.1-alpha.0**. Download the
optional dictionary ZIP from that application Release alongside its source ZIP.
The resource remains separately installed; the source archive does not contain it.

```powershell
python tools/external_assets.py verify output/amadeus-japanese-pronunciation-2026.10.20.3.zip
python tools/external_assets.py install output/amadeus-japanese-pronunciation-2026.10.20.3.zip
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
  The legacy CSV is loaded as supplied: the official pack's checked-reading
  ledger does not filter it. An old full upstream copy therefore retains its
  known shifted readings. To use the checked subset, install the official pack;
  to use only the base dictionary, set `ENABLE_JAPANESE_USER_DICTIONARY=false`
  and restart the backend. This preserves existing local-source compatibility.
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
没有正式包且本机存在旧 CSV 时，仍会加载整份旧词典，其中的已知错读不会被
正式包的审核清单过滤。安装正式包可改用审核子集；关闭
`ENABLE_JAPANESE_USER_DICTIONARY` 并重启后端可仅使用基础词典。
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
python tools/external_assets.py build japanese-pronunciation --output output/amadeus-japanese-pronunciation-2026.10.20.3.zip
python tools/external_assets.py verify output/amadeus-japanese-pronunciation-2026.10.20.3.zip
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

Version **2026.10.20.3** admits only individually checked, exact word/reading
pairs from `tools/data/japanese_dictionary_reviewed_readings.json`. The user
explicitly selected smaller coverage over distributing unchecked upstream data.
The ledger is a model-assisted lexical candidate review against CMU phones and
Japanese loanword usage, **not independent human or native-speaker sign-off**.
Independent maintainer review, accent and listening acceptance remain pending.

The candidate contains **129 rows / 127 distinct words**, selected from 129
ledger words. Base ownership and conservative screening can still exclude a
checked word. A checked alternative does not authorize other readings of the
same word. The loader rejects old full-data candidates lacking this ledger;
update that local pack or disable it. Historical upstream CSVs and the 317-row
review remain research inputs; they are not automatically admitted into the
formal pack. Personal/legacy sources remain an explicit, separate user choice.

Preparation applies these rules offline, after representation repairs:

- Yield words recognized by an independent pinned base MeCab instance, and
  redundant letter/initialism readings, to the base dictionary.
- Quarantine every variant of a surface if its final Katakana consonant
  conflicts with all CMU variants: ズ requires S/Z, ド requires D/T, ング requires
  NG. This screen can both miss errors and reject valid loanwords. It is a
  secondary screen, not permission to release all other upstream rows.
- Admit a remaining row only when **both** reading fields match an exact
  checked pair in the hash-pinned ledger. No automatic rough transliterator or
  runtime pronunciation exception decides lexical admission.
- Bound retained Latin surfaces with native fullwidth spaces; the frontend
  marks only complete tokens present in the registered official pack. `mode`
  cannot intrude into `model`, nor `con` into `icon`. Both frontend paths use
  the same contract. Unlisted words/units retain their input. Original costs
  are preserved; historical cost calibration and the common floor are retired.

Provenance contains the ledger identity and accepted pairs, source/CMU/base
hashes, aggregate exclusions and a reproducible hash of excluded audit rows.
The rejected source readings are not copied into the resource pack. Detailed
research evidence can be generated separately by the offline audit command.
The original correction history remains tracked; only approved corrections
are described in the pack. The source's 1,557 terminal-phone flags plus 112
variants remain excluded, but this is not a claim that all 1,669 were errors.

正式候选包仅收录逐条检查过的“词形＋读音”，接受覆盖缩小。同词形的未审核读音
也不收录。清单目前属于模型辅助检查，仍需独立维护者复核和人工试听；不再把
“未被自动规则标出”当作质量合格。没有新增运行时逐词特判。

The production `clean_text` gate independently loads the checked-in ledger and
asserts every released word in three contexts (**381 checks**) plus 127
frontend/prosody comparisons. All 129 stored rows must match approved readings,
including alternatives that native cost selection does not choose. The gate
also checks 52 baseline controls, 32 extra units, all 814 base-owned words in
three contexts, and **303 common English controls**. All **190 unlisted common
words** must preserve baseline phones and remain unmarked by token boundaries.
There is no coverage-only correctness claim for released words. The 26 reported
sentence contexts include the new interior/vowel-shift examples; those omitted
words fall back to the base, rather than receiving guessed replacements.

The candidate passes those lexical/integration gates with no unit-prosody
changes. `model`, `welcome`, `directly`, `reset`, `deadline` and the newly
reported unreviewed words may still be spelled as letters. This is a known
coverage limit. Existing correct help/learn/two/with/production remain owned by
the base; `let’s` uses the checked レッツ repair. Proper-name ambiguity and the
provisional `proposer` accent still require review. These checks establish
these targets only, not universal accuracy or natural emotional delivery.

The gate runs offline with a prepared candidate. Core tests use synthetic data
and require no dictionary/model downloads. Pack validation checks content hash
and exact ledger membership, and memoizes results by member stats. Audio cache
keys perform no dictionary-file validation. Abbreviation preparation, subtitles,
conversation history and the character checkpoint contract are unchanged.

## Publication / 发布

Publish the checked dictionary ZIP **as an attachment of the compatible
application Release**, alongside the source archive, checksum list and source
manifest. The Release records the tested application/dictionary pair and links
the exact versioned ZIP from the organization resource page. Start with
`v0.17.1-alpha.0` and dictionary `2026.10.20.3`.

Application and dictionary data retain independent version identifiers. A later
application Release may attach the same byte-identical dictionary when it still
passes that release's gates, or attach a newer checked dictionary. A resource
format change requires a new pack-contract version and an updated resource;
future application code is not obliged to retain retired pack contracts. Keep
old Release assets unchanged. No runtime dependency on a GitHub tag or a
specific dictionary data version is introduced. See the
[publication procedure](../release/README.md#5-publish-the-application-and-optional-resources-together).

词典与应用在同一个 Release 发布，分别提供 ZIP，保持可选安装。每次发布记录
实际验收的应用与词典组合；格式升级时随新版应用提供新资源，不要求新代码永久
兼容旧包。数据未变且通过本次验收时可以复用原 ZIP；词典修订使用新版本和新文件名。
旧应用与旧资源保留供原版本用户核验，语音、角色等既有大包无需重打。
