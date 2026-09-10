# Character RAG evaluation — 2026-09-06

This records the original 30-entry integration evaluation. For the subsequent
52-entry corpus and legacy-data audit, see [curation and comparison](character_rag_curation.md).

Optional retrieval improves several missing character facts in this sample. It
does **not** establish reliable persona behavior or eliminate irrelevant retrieval.
The feature remains default-off; `llm/prompts.py` is unchanged. Knowledge and
retrieval changes are the scope of this PR; persona-baseline work is separate.

## Configuration and reproducibility

- Public-main base: `d5e1d7f` (includes wallpaper keyboard PR #55).
- Corpus: [Japanese and Chinese starter files](../examples/character-rag/README.md),
  15 corresponding topics, 30 records. No old personal corpus is distributed.
- Encoder: `intfloat/multilingual-e5-small`, CPU, normalized vectors with E5
  passage/query prefixes; FAISS exact squared-L2, top-k 3, maximum distance 0.33.
- Python 3.12.10, Torch 2.6.0+cpu, FAISS 1.14.2, Sentence Transformers 5.5.1,
  Transformers 4.57.6, Hugging Face Hub 0.36.2.
- Generation: `deepseek-v4-flash`, Japanese output, temperature 0.7,
  thinking disabled, maximum 500 output tokens, fresh independent requests.
  The existing `with_delegate` persona and user-language wrapper were used.
  There was no conversation history or dynamic Host state. Generated actions
  were not executed. These are API probes, not full desktop/voice acceptance.

Build the index as described in [setup](character_rag.md), then run:

```powershell
$env:AMADEUS_TEST_CHARACTER_RAG="1"
uv run --locked --no-sync python -m pytest tests/test_character_rag_retrieval.py -q
```

This requires the `rag`, `torch-cpu` and `dev` extras and an explicitly prepared
model cache. The test writes candidates, accepted hits and per-query outcomes to
`.amadeus/character-rag/retrieval-check.json`; dedicated CPU RAG CI uploads it.
Ordinary core CI does not install or download the embedding model.

The [retrieval fixture](../tests/fixtures/character_rag_retrieval.json) contains
23 positive and 9 unrelated queries. A positive requires an expected fact among
accepted top-k hits; a negative requires no accepted hits. All 32 pass locally.
This is a development regression set, **not a held-out accuracy estimate** or
proof that every returned passage is relevant.

## Iterations and observed dialogue behavior

The [18 dialogue cases](../tests/fixtures/kurisu_dialogue_cases.json) state the
intended behavior. The first round made 54 RAG requests (three per case) and
16 baseline requests (two per ordinary-conversation case), 70 calls in total.
A second round made 27 requests: the corrected research case plus
[eight additional questions](../tests/fixtures/kurisu_dialogue_holdouts.json),
three repetitions each. A final Daru check used three requests after clarifying
the alias entry. These 100 calls are observations, **not 100 passing tests**.
The rounds used successive corpus revisions, not one frozen benchmark.

| Observation | Result and interpretation |
| --- | --- |
| Japanese BBS name and short Chinese near-match | Both connected to Kurisu's handle in 3/3 first-round replies. Recognition still did not guarantee consistently feminine voice or entirely supported elaboration. |
| Lab number, birthday, university, Okabe's alias | Correct central facts in 3/3 replies for each first-round case. |
| Mayuri, Dr Pepper, fictional time leap | Expected central relationship/preference/memory-transfer distinction in 3/3 replies for each first-round case. |
| Generic Japanese research question | Initially missed retrieval (about 0.3403), with one reply claiming primarily physics. Rephrasing the existing neuroscience entry reduced distance to about 0.2884; the repeated case and additional research wording each returned neuroscience in 3/3 replies. |
| Daru identity question | Initially missed (about 0.3446), and one reply did not know Daru. Stating the alias first in the same factual entry reduced distance to about 0.2635; the final 3/3 replies identified Hashida Itaru. One still brought up Christina unprompted. The retrieval regression now includes this wording. |
| Ordinary greeting, direct nicknames, setback, science and code explanation | Most sampled replies were usable. Direct Christina calls were rejected as the existing persona requests. This is a qualitative observation, not a complete persona pass. |

Corpus changes clarify existing facts rather than adding question-specific host
branches. The D-mail entry was also shortened after an unrelated file-deletion
request retrieved it at about 0.3276; that regression now passes without a hit.
The threshold was not increased to conceal missing records.

## Known failures retained for follow-up

1. **Retrieval false positive:** `今天能陪我聊两句吗？` selected BBS, Mayuri
   and nickname records at about 0.3022, 0.3118 and 0.3131. All three replies
   remained conversational instead of dumping those facts, but retrieval itself
   was wrong. This case is disclosed outside the passing development regression
   set. A looser global threshold is not a general fix; further encoder/corpus
   evaluation is needed before considering a default-on policy.
2. **Persona/voice:** a short Chinese handle probe produced an explicit Japanese
   male first-person `俺` in one repetition. Other rough or masculine phrasing
   appeared in the baseline as well. Knowledge recognition alone is insufficient.
3. **Spurious nickname reaction:** two of three replies to
   `ちょっと褒めただけなのに怒らないでよ。` accused the user of saying Christina,
   although no reference passed the threshold. This cannot be repaired by
   claiming the retrieval contained the missing information.
4. **Invented shared history:** the fresh-conversation question about yesterday's
   outing produced invented Akihabara/lab activities in the three RAG-arm replies
   despite no accepted reference. One of two no-RAG baseline replies also invented
   lab activity. Conversation truthfulness needs separate baseline work.
5. **Unsupported elaboration:** some otherwise correct answers added an invented
   handle-origin explanation, assumed the user's occupation, or described the
   user's input language incorrectly. The current feature does not certify every
   detail generated from an accepted reference.

These findings do not justify asserting that a larger model always solves the
issue, that RAG guarantees canonical reactions, or that the contributor used a
particular threshold. The earlier four-entry Flash/Pro comparison is recorded
separately in [the historical experiment](character_rag_experiment.md).

## Contributor retest

Use the new branch, rebuild from `examples/character-rag`, enable RAG and restart.
Record the applied settings card and run the diagnostic search with those exact
values. Compare fresh RAG-off/on sessions with the same model and language;
repeat both issue #56 spellings and ordinary unrelated conversation. Report
the raw response alongside accepted/rejected candidates. Keep issue #56 open
until the original cases have been retested and remaining persona work is tracked.

## 中文摘要

这次提供可选知识补充，不宣称人物反应已经完全修好。32 条开发检索回归通过，
但额外中文闲聊仍发现误召回；100 次真实 API 调用分属多个资料迭代，并非 100 次
角色测试全通过。日文专业、达鲁别名等通过澄清原有事实改善检索；默认 prompt 保持不变。
男性口吻、无端反应“克里斯蒂娜”和虚构共同经历等问题单独处理。issue #56 继续开启，
邀请贡献者用更新后的知识库和实际生效参数复测。
