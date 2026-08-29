# GPT-SoVITS vendored source provenance

This record covers the source copied into Amadeus, not separately supplied
model weights, reference voices, training data, or character assets. It
preserves facts that cannot be represented by the GPT-SoVITS root MIT file
alone. It is an engineering provenance record, not legal advice.

## Closest verified import baseline

- Initial Amadeus import: `5ac75df98bb9e1887235b7e296ab3ce1721f0603`.
- Closest verified GPT-SoVITS baseline: RVC-Boss/GPT-SoVITS commit
  `9da7e17efe05041e31d3c3f42c8730ae890397f2`, dated 2025-04-01.
- Scope compared: `GPT_SoVITS/**`, `tools/asr/**`, `tools/i18n/**`,
  `tools/uvr5/**`, `tools/my_utils.py`, and `tools/subfix_webui.py`, excluding
  the separately inventoried `GPT_SoVITS/BigVGAN/**` tree.
- Of 173 files in the initial Amadeus scope, 155 have the same Git blob as the
  baseline, 11 differ, and 7 exist only in Amadeus. The baseline contains 30
  additional files that were not imported.
- Since that import, 33 non-BigVGAN paths in this scope have changed in
  Amadeus. The vendored tree is therefore recorded as modified rather than as
  an unmodified upstream snapshot.
- `LICENSES/GPT-SoVITS-MIT.txt` has the same Git blob as the license at the
  verified baseline.

The comparison identifies the closest reproducible base; it does not claim
that the 18 non-identical/local-only files came from that commit without
further modification.

## Included source families and retained notices

| Local source family | Recorded upstream source | License evidence |
| --- | --- | --- |
| `AR/modules/activation*`, `embedding*`, and `transformer*` | [lifeiteng/vall-e](https://github.com/lifeiteng/vall-e) | Apache-2.0; full text in `LICENSES/Apache-2.0.txt` |
| `AR/modules/optim.py` and `scaling.py` | Xiaomi/Daniel Povey code carried through VALL-E/SoundStorm | Apache-2.0 headers remain in each file |
| `AR/modules/patched_mha_with_cache*.py` | PyTorch multi-head attention | BSD-3-Clause; notice in `LICENSES/PyTorch-BSD-3-Clause.txt` |
| `module/core_vq.py` and `module/quantize.py` | [Meta EnCodec](https://github.com/facebookresearch/encodec) and [vector-quantize-pytorch](https://github.com/lucidrains/vector-quantize-pytorch) | MIT; Meta and Phil Wang notices are retained in `core_vq.py`, and the upstream links are retained here for `quantize.py` |
| `module/**` VITS/vocoder portions | [jaywalnut310/vits](https://github.com/jaywalnut310/vits), [TransferTTS](https://github.com/hcy71o/TransferTTS), and [HiFi-GAN](https://github.com/jik876/hifi-gan) credited by GPT-SoVITS | MIT; the RVC-Boss root MIT and upstream credits are retained |
| `f5_tts/**` | [SWivid/F5-TTS](https://github.com/SWivid/F5-TTS) | MIT for source code. The copied module also cites BigVGAN (MIT), rotary-embedding-torch (MIT), ConvNeXt-V2 source code (MIT), and diffusers (Apache-2.0). No F5 model weight is included in the source archive. |
| `text/zh_normalization/**`, `text/tone_sandhi.py`, and the Paddle/GitYCC-derived G2PW files | [PaddleSpeech](https://github.com/PaddlePaddle/PaddleSpeech) and [GitYCC/g2pW](https://github.com/GitYCC/g2pW) | Apache-2.0; Paddle copyright/license headers remain in the copied files and the standard text is retained centrally |
| `text/g2pw/g2pw.py` | [pypinyin-g2pW](https://github.com/mozillazg/pypinyin-g2pW) | MIT, copyright 2022 mozillazg |
| `text/japanese.py` | [CjangCjengh/vits](https://github.com/CjangCjengh/vits) and copied phoneme routines from [ESPnet](https://github.com/espnet/espnet) | MIT for the VITS portion; Apache-2.0 for ESPnet |
| `text/korean.py` | [MB-iSTFT-VITS-Korean](https://github.com/ORI-Muchim/MB-iSTFT-VITS-Korean) and [g2pK](https://github.com/Kyubyong/g2pK) | Apache-2.0 |
| `text/cmudict.rep` and its compact derivative | CMUdict 0.07b, SVN revision 13083 | BSD-2-Clause-style CMU notice retained inline and in `LICENSES/CMUdict-BSD-2-Clause.txt` |
| Language-splitting project credit | [DoodleBears/split-lang](https://github.com/DoodleBears/split-lang) | MIT, copyright 2024 DoodleBear. The local `split_lang.py` was local-only in the import comparison, so this records the upstream project credit without claiming blob identity. |
| `tools/my_utils.py` audio-loading reference | [OpenAI Whisper](https://github.com/openai/whisper) | MIT, copyright 2022 OpenAI |

MIT source notices represented above include copyrights held by RVC-Boss,
Jaehyeon Kim, Meta Platforms, Phil Wang, Yushen CHEN, Jungil Kong,
DoodleBear, mozillazg, and OpenAI. Apache-2.0 sources use the complete standard
text in `LICENSES/Apache-2.0.txt`; their file-level copyright headers remain
authoritative where present.

## Material excluded from the public source archive

### Cantonese frontend

`GPT_SoVITS/text/cantonese.py` identifies the Hugging Face Space
`Naozumi0512/Bert-VITS2-Cantonese-Yue` as its source. That Space carried
AGPL-3.0 from its initial source commit
`7a6bcefd93b3eaefa370d4ab87c7801c61357cd1` on 2024-03-31. A line-based
comparison against that revision finds 164 aligned identical lines and a
similarity ratio of 0.796 after removing the local source-comment line. This
is treated as a substantive derivative, not as a citation-only reference.

The intended Amadeus public license does not absorb this file. The source
release policy excludes it, while a private checkout may retain the existing
Cantonese compatibility path. The complete upstream AGPL-3.0 text is retained
in `LICENSES/Bert-VITS2-Cantonese-Yue-AGPL-3.0.txt`.

### Generated or unverified dictionaries and caches

The public source archive excludes `GPT_SoVITS/text/ja_userdic/**` because no
source or redistribution record accompanies the 38 MiB custom dictionary.
The Japanese frontend already treats the user dictionary as optional.

It also excludes `GPT_SoVITS/text/**/*.pickle`. These are generated lookup
caches: English and G2PW rebuild their caches from tracked text dictionaries,
the named dictionary falls back to an empty mapping, and
`cmudict_cache.pickle` has no current reader.

### UVR5 utility tree

`tools/uvr5/**` is an upstream GPT-SoVITS WebUI utility and has no caller from
the Amadeus Electron/server/TTS runtime. It remains available in the private
checkout but is excluded from the public Amadeus source archive rather than
presented as part of the supported runtime.

## Known unresolved included chains

The AR model files below trace through RVC-Boss/GPT-SoVITS to
`yangdongchao/SoundStorm` commit
`da29b726f7b08db881606c768d90415ff6826753`:

- `AR/models/t2s_lightning_module.py`
- `AR/models/t2s_lightning_module_onnx.py`
- `AR/models/t2s_model.py`
- `AR/models/t2s_model0.py`
- `AR/models/t2s_model_onnx.py`
- `AR/models/utils.py`
- `AR/modules/lr_schedulers.py`
- `AR/text_processing/phonemizer.py`
- `AR/text_processing/symbols.py`

The SoundStorm repository contains no license file. Its corresponding files
attribute portions further to `feng-yufei/shared_debugging_code`, which is not
publicly retrievable and has no verifiable license record. No MIT permission is
inferred for that chain merely because GPT-SoVITS has a root MIT file.

Separately, `text/opencpop-strict.txt` exactly matches the file first added to
GPT-SoVITS at commit `41ca6028d6b5a672813e29bd7a671dafa5e7475a` but carries no independent
source notice. It is required by the Chinese frontend, so it is recorded as an
included unresolved data-origin question rather than silently excluded.

For this release, Amadeus relies on the repository-wide MIT grant of the
immediate RVC-Boss/GPT-SoVITS source while preserving these two original-source
caveats. It does not invent a standalone SoundStorm license or erase the
`opencpop-strict.txt` uncertainty. The engineering decision and its limits are
recorded in `LICENSES/GPT-SoVITS-UPSTREAM-RELIANCE.md`; under that stated policy
the included `gpt-sovits` component is release-ready. A distribution requiring
stronger original-source evidence must reopen this decision.
