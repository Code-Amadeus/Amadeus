# Chinese tokenizer dependency validation (D1)

D1 replaces the three GPT-SoVITS `jieba_fast` imports with the already pinned
`jieba==0.42.1` and removes `jieba-fast==0.53` from all local-model dependency
selections. This is a dependency-boundary change; no adapter, fallback, runtime
branch, dictionary modification or upstream source refresh is added.

**Compatibility decision after review: retain jieba's grouping and apply the
existing three-tone sandhi rule to it.** This does not preserve identical
pronunciation for every Chinese input. For example,
real `clean_text("憭怃憯。", "zh", version)` changes
`l iao3 w u3 c an3 .` to `l iao3 w u2 c an3 .` for v1, v2, v2Pro,
v2ProPlus and v3. Normalized text and `word2ph` remain identical. The old grouping
bypassed the existing rule; the new 1+2 grouping follows its intended branch.
The regression suite now records this behavior across all five model routes.
No listening or synthesized-audio acceptance was performed.

## Checked sources and environment

- Untouched public-main baseline: `bf0d0fae9baff5470769b60fc6ed3c277ba87f4e`.
- Checked on Windows AMD64, CPython 3.12.10, with jieba 0.42.1, jieba-fast 0.53,
  cn2an 0.5.24, pypinyin 0.55.0, ONNX Runtime 1.26.0, Transformers 4.57.6 and
  Torch 2.6.0+cu124. Actual G2PW inference used `CPUExecutionProvider`.
- The live imports are `text/chinese.py` (v1), `text/chinese2.py` (other listed
  versions), and shared `text/tone_sandhi.py`. Frontend segmentation uses
  `posseg.lcut`; `_split_word` uses `cut_for_search` for three-character sandhi.
  There is no production caller of the packages' raw `lcut` API in this path.
- Baseline/current comparisons loaded the three exact baseline source files
  from Git and the changed files in separate Python processes. Both used the
  same separately staged assets. The changed process blocked `jieba_fast` imports.
  Existing application environments were not synchronized or modified.

## Observed behavior

The retained offline regression probe exercises real normalization, POS,
pronunciation corrections, tone sandhi, erhua, phonemes, `word2ph` and symbol IDs.
Only the external G2PW ONNX converter is replaced with fixed pypinyin readings
in this deterministic test. Its snapshots are not neural-model evidence.

| Check | Observed result |
| --- | --- |
| Deterministic frontend snapshots | 14 synthetic/repository sentences x 5 versions: all 70 rows match baseline, with 6 segmentation/POS/search cases and 14 direct sandhi cases also matching |
| Actual G2PW, same 14 sentences | All 70 normalized text / phones / word2ph / symbol-ID rows match; all 14 raw pinyin rows match |
| Actual G2PW, additional common names/phrases | 李小敏、尹伟勇、马晓雨、许小雨、冷雨雪、好想你: all 30 rows match across the five versions |
| Raw repository fragments | 838 distinct Chinese fragments, 23,945 characters, from 26 tracked `GPT_SoVITS/text` Python files; 2,514 API comparisons: no POS differences, but 36 raw lcut/search differences across 18 fragments |
| Shared dictionary/HMM resources | `dict.txt` and seven finalseg/posseg probability/state resources are byte-identical |
| Positive-frequency three-character dictionary words | All 131,321 have identical search cuts |
| Three-character word/POS pairs from normalized repository fragments and sandhi premerge | 281 pairs checked; 38 split-group differences and 11 final-tone differences; the corpus includes character-conversion tables and unpronounceable sequences, for which existing errors were observed on both paths |
| Actual G2PW, 11 extracted rare sequences | v1 has 11 changed phone rows; each v2-family route has 10. Normalization and word2ph match for every row; raw G2PW readings also match |

The raw-package differences are not safely described as unreachable. Normalization
retains the rare characters and POS/premerge can send them to the three-character
search-cut boundary. For example, jieba-fast returns the whole unknown sequence
`憭怃憯`, so `_split_word` produces `["憭怃憯", ""]`; jieba produces character
cuts and the existing helper produces `["憭", "怃憯"]`. The unchanged sandhi
algorithm then changes the middle third tone to second tone. `揜揝揞。` is another
real frontend counterexample (`an3` becomes `an2`). Retaining the old empty
second group would preserve a bypass of the existing 1+2 sandhi rule. The direct
substitution therefore keeps the new grouping rather than adding an exception
for the legacy output. This is a mechanism-based compatibility decision; it does
not certify subjective pronunciation quality or bound all possible changes to
the sampled rare sequences.

The real-G2PW matching 14-case result files have identical SHA-256:
`4b582c69bc5ef50dfb248ec293b5a89c785d75e083cfa16c5e533729da172883`.
The ignored local `build/d1-validation/` directory retains the source-loader
harness, before/after JSON, corpus boundary differences, resource hashes and
latency batches. It contains no private conversations or reference voices.

## Actual-model assets

The real-model check used the model archive URL already recorded by the vendored
loader: [G2PWModel 1.1](https://www.modelscope.cn/models/kamiorinn/g2pw/resolve/master/G2PWModel_1.1.zip).
The archive was 588,857,174 bytes, SHA-256
`b116f6930a7ee55eef6576a8d8e14bf40c1106583439e8ae924b901512379c64`;
`g2pW.onnx` was 635,212,732 bytes, SHA-256
`2eb3c71fd95117b2e1abef8d2d0cd78aae894bbe7f0fac105ddc9c32ce63cbd0`.
Only the six files consumed by the converter were staged: the ONNX model,
`config.py`, both character lists, and both bopomofo conversion dictionaries.
The existing RoBERTa `config.json` and `tokenizer.json` loaded successfully with
AutoTokenizer; no RoBERTa weights or extra vocabulary download was needed.
Assets and model-loader path redirection remain outside tracked source files.

## Warm tokenizer latency

Seven alternating-order batches per package and operation used the same 14
normalized sentences, 20 repetitions each (280 calls per batch), after warmup.
Median measurements on this machine:

| Operation | jieba-fast batch / call | jieba batch / call |
| --- | --- | --- |
| lcut | 8.633 ms / 0.0308 ms | 22.717 ms / 0.0811 ms |
| posseg.lcut | 156.684 ms / 0.5596 ms | 152.724 ms / 0.5454 ms |
| lcut_for_search | 6.709 ms / 0.0240 ms | 13.501 ms / 0.0482 ms |

This small local probe does not establish cross-machine latency or end-to-end TTS
performance. Raw lcut is not called by this frontend; POS and short-word search
are the relevant boundaries.

## Package and regression checks

- `uv lock --check --offline` passes. Only jieba-fast project edges and its now
  unreachable package/artifact record were removed; all 285 remaining third-party
  package records and artifact hashes, and lock-wide settings, are unchanged.
- `python -X utf8 -m pytest -q tests/test_local_tts_chinese_frontend.py`:
  The original 77 cases passed; five additional regression cases now pin the
  reviewed unknown-word sandhi behavior. The five T4 build exports retain jieba
  0.42.1 and omit jieba-fast,
  including their applicable Windows/Linux/macOS markers. The new tests are
  included in cu124, candidate-build and ROCm T4 CI lists.
- All local_tts, gpt_sovits and t2s test files: 290 passed, 1 skipped (the explicit
  NVIDIA kernel probe). Release/core-install/environment-verifier tests: 36 passed.
  New test/probe Ruff checks pass.
- A fresh tokenizer-only environment installed the exact locked jieba sdist
  (SHA-256 `055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2`),
  cn2an, proces and pypinyin. During the source build PATH contained only
  System32 and CC/CXX named a nonexistent compiler. No jieba binary extension was
  installed and jieba-fast was truly absent. All 70 deterministic frontend rows,
  segmentation cases and sandhi cases matched the saved baseline in this environment.

The tokenizer source-build result does not qualify a clean whole-T4 Windows
installation without Build Tools. crcmod's optional extension and the v3
BigVGAN/nvcc path still need separate qualification, as do actual synthesized
audio, listening and GPU execution. Hosted platform CI is recorded on PR #308;
it passed all 18 checks before the review follow-up, and changed heads require
fresh checks. The clean Windows installation gate remains tracked in #268.
No claim is made that T4 as a whole is compiler-free.
