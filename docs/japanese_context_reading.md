# Optional Japanese contextual readings / 可选日语语境读音

`japanese-context-reading` adds upstream tsqyomi v5 reading selection to the
existing Japanese OpenJTalk frontend. It chooses among native reading candidates
before NJD computes morae, accent and phrase connections. Original Japanese text
is retained. This is independent of the optional English loanword dictionary,
voice weights and emotion references; it is not a new semantic emotion planner.

The pinned model comes from `tsukumijima/tsqyomi-models`, revision
`81add61ddba9669d328e307c883d05d77d60f5f4`, directory `v5/`. The native API is the
existing `pyopenjtalk-plus==0.4.1.post9`; no replacement G2P library is required.
The model, tokenizer and metadata hashes are fixed together in
`config/japanese_reading.py`. Source/license limits are recorded in
`LICENSES/TSQYOMI.md`; unchanged model files remain outside Git/source archives.

## Install and control

Install a compatible application version before the independent resource pack:

```powershell
python tools/external_assets.py verify output/amadeus-japanese-context-reading-2026.10.20.1.zip
python tools/external_assets.py install output/amadeus-japanese-context-reading-2026.10.20.1.zip
```

The Voice setting **Use installed context-reading model** maps to
`ENABLE_JAPANESE_CONTEXT_READING`. It defaults to allowing an installed model,
but never installs/downloads one. Missing optional data is normal and inactive.
Restart the backend after installing, updating or changing this setting. Turning
it off uses the existing native reading selection; the English dictionary has
its own independent switch. Invalid/incomplete data or unsupported native APIs
are reported and leave the existing frontend usable.

安装资源或更改“语音 → 日语语境读音”后须重启后端。未安装时不会自动下载，
仍使用原前端。语境读音模型和英文词典可以独立安装、独立关闭。

Startup verifies all three source hashes and loads only local files, explicitly
on CPU. It does not contact Hugging Face or choose a GPU provider. The loaded
model identity joins the actual native dictionary identity in first-sentence
audio-cache keys and in the existing sidecar ready/done status. Disabled or failed
model activation cannot reuse model-conditioned audio as base audio.

Both plain-phone and prosody frontend paths use the selected native readings.
Checkpoint symbol IDs remain unchanged: the existing frontend's `#` boundary
mapping to its UNK ID is retained rather than adding an incompatible embedding
entry. This compatibility fact is separate from unknown lexical phonemes.

## Offline preparation and validation

Obtain the three pinned `v5/` files separately; startup does not fetch them.

```powershell
python tools/prepare_japanese_reading.py --source path/to/pinned/v5
python tools/validate_japanese_reading.py --output output/japanese_reading_validation.json
python tools/external_assets.py build japanese-context-reading --output output/amadeus-japanese-context-reading-2026.10.20.1.zip
```

The six-member pack contains the unchanged ONNX model, tokenizer and metadata,
provenance, source notice and MIT terms. The existing installer verifies the ZIP
manifest and installs those files. Native activation additionally checks the
fixed upstream hashes. Preparation refuses a different model or a differing
existing destination unless `--overwrite` is explicit.

`tools/data/japanese_reading_cases.json` provides 88 synthetic, contrastive reading
targets, including ordinary readings that must not regress. The production-path
gate compares complete native nodes, checks mora counts and symbol compatibility,
and compares plain/prosody paths. It tests correctness against expected readings,
not merely equality before/after installation.

## Experiment, 2026-10-20

Windows AMD64, Python 3.12.10, pyopenjtalk-plus 0.4.1.post9, ONNX Runtime 1.26.0
and tokenizers 0.22.1 were used for the local comparison. The same 88 lexical
targets and accepted readings were used for each engine. Haqumei used its default
frontend, not Amadeus-specific adaptation. These are curated diagnostics, not an
independent estimate of general Japanese accuracy.

| Reading selector | Correct targets | Decision |
| --- | ---: | --- |
| Existing frontend (Sudachi / nani defaults) | 69 / 88 | Retain as the base path |
| tsqyomi v4 (published native default model) | 82 / 88 | Reject: regresses ordinary 上手 / 下手 / 生 readings |
| Haqumei 0.12.2 | 69 / 88 | Reject replacement: no net gain on these targets |
| Pinned tsqyomi v5 | 88 / 88 | Optional installed model |

The v4 snapshot was `680596dd2ad2bad59ee5db3741e197cfce79f9b4`; v5 is pinned
above. The checked frontend upstream commit was
`b17704eed5f8a7eef778310f9eb9a7f71fb5321b`; it also selects v5. Haqumei's
repository was checked at `e70fa6279a225c26f8956ce393730ca58f86db7e`.
The additional 32 contexts were frozen before the extended v5 measurement.
The gate compares whole native nodes: for example, a merged 何と is tested as
何と / ナント, rather than incorrectly demanding a separate 何 node.

A fresh six-file install into a Japanese path containing spaces passed the
actual production `clean_text` gate: base 69/88, model 88/88, no lost correct
targets, mora mismatches, unsupported phonemes or plain/prosody disagreements.
Warm CPU median was 6.79 ms (base 0.33 ms), p95 7.77 ms; model loading added
0.76 seconds. These are single-machine observations, not performance guarantees.
The existing English-dictionary regression gate also passed with both packs
installed. Long inputs of 100 repeated / 80 mixed clauses completed natively.

Three sentences were synthesized with the same locally configured V3 character
voice, reference and sampling seed (237), both with and without context reading:
ordinary 上手, stage-side 上手, and deserted-place 人気. All six waveforms were
nonempty and finite at 24 kHz. These private local outputs are not part of the
resource/source release, and have not received listening acceptance.

Native checks do not prove exhaustive accuracy, appropriate character emotion or
human listening quality. Ambiguous readings accept multiple valid forms; proper
names and readings outside the model/candidate vocabulary remain limited.
Waveform comparisons use the same voice, reference and sampling seed. Their
generation/integrity does not replace listening acceptance. The optional resource
ZIP is kept local until separately approved for publication.
