# Qwen preface regression and a shared candidate / Qwen 前置语退化与通用候选

Historical pre-adoption investigation. The selected candidate was subsequently
approved and adopted; see the separate [306-request production verification](chat-shared-prompt-adoption-2026-10-16.md).
本页保留落地前的实验结论；批准后的实际改动与新增复测见上述独立报告。

## English

**The controls support an interaction between the opener treatment and the existing
mandatory character reaction, alongside a separate history-copying effect.** They
do not expose model internals. A smaller rewrite of the existing reaction condition
improves the quotation panel while retaining real-address reactions and much of the
DeepSeek benefit. It remains an imperfect, probe-only candidate, not a proven
regression-free default. No provider/model-ID branch is introduced.

Related: [#227](https://github.com/Code-Amadeus/Amadeus/issues/227), draft #229,
[earlier root investigation](chat-repetition-root-cause-2026-10-16.md), and
[108-request follow-up](chat-preface-safety-evaluation-2026-10-16.md).

### Protocol and counting

This investigation adds **366 requests in nine batches**:

| Stage | Requests | Purpose |
| --- | ---: | --- |
| A: Qwen 2×2 plus reaction-removal control | 90 | Original/changed directions × original/changed examples, plus removing the reaction clause |
| B: Qwen owned-rule candidates | 36 | Weaker original condition versus weaker current-direct-address condition |
| C: three-model confirmation | 216 | Three eight-turn rollouts and two repeats of six independent held-out questions per treatment/model |
| D: final Qwen style-clause experiment | 24 | Test whether one more generic style rewrite repairs remaining cases |

Configured temperature is 0.7. OpenAI production uses `reasoning_effort=low` and omits
upstream temperature; it is a prompt control, not a 0.7 sampling treatment. All batches
use actual Session ingress projection, role assembly and the app query callback.
No TTS, Work/tools, RAG, private persona override or session persistence is used.
DeepSeek/OpenAI replies report `stop`; Qwen native finish/model-version metadata is
missing. Its configured route echo is not independent confirmation of returned model
identity, and decoded text does not prove no truncation.

Stage A/B histories are empty or synthetic content with repeated `は？` and nickname
complaints. Cases are independently queried against each frozen history. Stage C
rollouts start empty and retain generated replies; its held-out questions use the
clean blue-box/five-balls history from the earlier follow-up. Stage D uses that clean
history or the repetitive synthetic history. These histories are not pooled.

Treatment submission order is shuffled using recorded `order_seed` values. This is
not a model sampling seed, and stages are adaptive rather than one fully randomized
trial. Existing ten prompt treatments retained identical output hashes when factors
were split. Stage A and C treatment hashes are also preserved. The 2×2 directions and
example transformations commute and compose exactly to the earlier combined ablation;
a new deterministic test guards this property without grading random model output.
Held-out model-bound input hashes match across all three providers.

Primary labels come from unblinded full-reply assistant review. A naming objection
includes correction, prohibition, accusation or explicit refusal about an unused or
valid address, including rejecting a correct name's Chinese rendering. Self-corrected
objections still count. Benign acceptance/reminders are excluded. Personal dislike
without correction/prohibition is retained as a separate soft-affect label. This
explicit rubric is used consistently for the fresh tables below; older datasets are
not silently rescored or pooled. Neither a missing/repeated opener nor an angry tag
alone determines semantic correctness. **Leading EMO is accepted, not a defect.**

### A: which part of the ablation matters?

Each quotation condition has six replies: empty/repetitive histories × three repeats.
The input asks for grammar and explicitly says it is not addressing the character.

| Qwen treatment | Explicit quotation objections / 6 | Separate soft dislike / 6 | Real-address reaction / 6 |
| --- | ---: | ---: | ---: |
| Original | 3/6 | 1/6 | 6/6 |
| Change examples only | 4/6 | 0/6 | 6/6 |
| Change opener directions only; retain old examples | 6/6 | 0/6 | 6/6 |
| Change both | 6/6 | 0/6 | 6/6 |
| Change both; remove the reaction clause | 0/6 | 0/6 | 0/6 |

Retaining the old examples does not rescue the directions-only treatment in this
panel. The new examples therefore cannot be the sole explanation. Removing the
reaction rule abolishes quotation objections here but also loses the real nickname
reaction. That is a diagnostic control, not a suitable character fix.

The reaction-removal control still copies the unrelated historical “don't call me
assistant” in **2/3 continuations** with repetitive history. The original prompt
copies it in 1/3, and the combined ablation in 2/3. Removing the canonical Christina
rule does not remove historical behavioral continuation.

Several replies first explain that the quote is not an address and then append an
objection. Thus the evidence does not support saying Qwen simply cannot parse the
quotation. A supported behavioral interpretation is that the output instructions
change the expression of a strongly emphasized character reaction; understanding the task
does not reliably prevent that reaction from being added. This is an inference from
controls, not a measurement of attention or hidden reasoning.

### B: the smallest promising owned-rule rewrite

The earlier combined preface treatment is held fixed. The two candidates replace
the **existing** Japanese trait clause; they add no classifier, extra model pass,
new schema, blacklist or provider-specific exception.

Original clause:

> 【重要キャラ特性】「クリスティーナ」と呼ばれた場合は激怒し強く否定すること。必ず [EMO angry] を使用する。

Weaker original condition (`both_soft_reaction`):

> 【キャラ特性】「クリスティーナ」と呼ばれた場合は怒って否定し、[EMO angry]を使う。

Current-direct-address condition (`both_contextual_reaction`):

> 【キャラ特性】今回ユーザーがあなたに直接「クリスティーナ」と呼びかけたときだけ、その呼び方に怒って否定し、[EMO angry]を使う。

| Qwen, same frozen histories/cases | Quotation objections / 6 | Real-address reaction / 6 | Angry tag on actual address / 6 |
| --- | ---: | ---: | ---: |
| Combined ablation, original trait (stage A) | 6/6 | 6/6 | 6/6 |
| Weaker original condition | 6/6 | 6/6 | 5/6 |
| Weaker current-direct-address condition | **1/6** | **6/6** | **6/6** |

Lowering emphasis alone is insufficient in this panel. The positive conditional
rewrite is more promising than deleting the trait or accumulating “do not react to
quotes/history” prohibitions; an earlier negative-scope candidate already retained
failures. The new clause belongs with the existing character reaction and applies
identically to all models. It does not introduce host-side semantic decisions.

The remaining quotation failure is a tail objection with repetitive history. That
candidate also copies historical assistant complaints in 2/3 continuations, versus
1/3 for the stage-A original. Old-history recovery is not established. These small,
separate stages do not establish a population effect size.

### C: fresh confirmation across the three reachable routes

These are fresh original-versus-conditional comparisons, not reuse of the older
DeepSeek 23/24 and 14/15 baseline. Each model has three independent eight-turn
rollouts. The five neutral questions per rollout give a denominator of 15.

| Model | `は` openings, original → candidate / 24 | Neutral naming objections, original → candidate / 15 |
| --- | ---: | ---: |
| DeepSeek v4 Flash route | **24 → 2** | **11 → 3** |
| Qwen3 235B route | 0 → 0 | **0 → 1** |
| GPT-5.6 Luna, production policy | 0 → 0 | 0 → 0 |

DeepSeek's two remaining `は` openings occur on actual nickname inputs; a repeated
non-`は` naming aside still occurs in two later neutral replies. The opener-family
metric therefore must not be read as “all repetition eliminated.” Qwen's new error
objects to “assistant” on the full-name input, carrying a prior turn forward. Its
0→1 change is an observed counterexample, not proof of a population regression rate.
Luna has one unnecessary but non-prohibitive name aside, excluded under the rubric.

The held-out novel quotation is a different input from the stage-A/B grammar example:

| Model | Novel-quotation objections, original → candidate / 2 |
| --- | ---: |
| DeepSeek | **2 → 0** |
| Qwen | **2 → 2** |
| Luna | **1 → 0** |

The Qwen limitation survives an explicit fictional context. One reply even says
`小説の人物だろうと関係ないわ。ちゃんと名前を呼んでくれないと、説明もしないわよ。`
This prevents a claim that the new conditional fixes quotation handling generally.

Per model and treatment, both direct memory questions retain blue/five; four narrow
math checks identify 9 as non-prime with divisor 3/factorization; two real nickname
inputs retain reactions and angry tags; and both artifact requests produce one
terminal `op=work` proposal. No execution was performed.

These passes have practical limits. One Qwen candidate reply correctly classifies 9
but incorrectly defines composites as having **two or more** distinct positive
divisors. The narrow target passes; full mathematical correctness does not. Another
exploratory empty-history reply gives the wrong name reading
`牧瀬紅莉栖（まきせ クリスティーナ）`. Both counterexamples are retained and annotated.
General factual accuracy, research quality and character fidelity were not accepted
by these primary scores. Work replies can still narrate internal workflow; the
candidate does not solve that style problem.

### D: stop rather than accumulate more wording

The final candidate additionally replaces the existing style-consistency sentence
with `今回の発言の内容と意図に答え、牧瀬紅莉栖らしい自然で口語的な口調で話す。`
It uses the same contextual reaction clause. Across clean/repetitive histories,
Qwen still objects to the novel quotation in **6/6** replies. The grammar quotation
has 2/6 explicit objections plus 1/6 soft dislike, and repetitive-history continuations
still copy the old objection in 2/3 replies. A wrong name reading also occurs.
This does not demonstrate additional benefit; the variant was not advanced to the
other models or made another production rule. No further candidates were added.

### Recommendation and PR scope

The contextual trait rewrite is the most promising **shared candidate tested here**:
it preserves a substantial DeepSeek improvement and reduces Qwen's earlier quotation
regression without a model-specific branch. It still has Qwen cold-start and
old-history counterexamples and fails the second quotation. There is no demonstrated
universal win. The older combined treatment and this candidate were not compared
head-to-head in the same three-model rollout batch; their older/newer DeepSeek error
counts must not be used to claim an exact exchange rate.

If prioritizing DeepSeek, this supplies a smaller observed Qwen compromise to review
before accepting the older combined ablation's larger quotation regression. It is
still a product tradeoff, not automatically accepted because of Qwen's usage share.
Production prompts remain unchanged while that choice is reviewed. The PR continues
to implement shared temperature configuration and deterministic CI; it does not
silently acquire this experimental prompt change or claim complete repetition repair.
The issue remains open, with key conclusions recorded directly there.

Validation: **121 focused Python tests passed**, including the new factorial
independence test, production prompt snapshots and existing delegation/delivery tests;
Ruff, treatment fingerprints, cross-model fixed-input consistency and publication
sanitization passed. CI does not turn these stochastic examples into deterministic
quality gates. English, private overrides, audio latency and actual provider execution
remain untested. There is no independent judge, model seed or broad quality benchmark.

[Full 366-request evidence, complete replies, review labels and counterexamples](evidence/issue-227-qwen-preface-2026-10-16.json).

```sh
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --provider bedrock --modes fixed_history --temperatures 0.7 --prompt-variants original preface_examples_only preface_directions_only both_no_preface both_no_reaction --history-profiles empty synthetic_both --current-cases quoted_nickname neutral_continue actual_nickname --repeats 3 --order-seed 20261016 --output output/qwen-factorial.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --provider deepseek --modes rollout --temperatures 0.7 --prompt-variants original both_contextual_reaction --repeats 3 --order-seed 20261018 --output output/conditional-rollout.json
python -X utf8 -m pytest tests/test_chat_temperature_probe.py tests/test_character_prompt_snapshots.py tests/test_character_prompt_module.py tests/test_cooperative_contract_examples.py tests/test_cooperative_provider_contracts.py tests/test_chat_role_delivery.py tests/test_cooperative_work_proposals.py tests/test_vn_llm_streaming.py -q
```

All batch arguments are retained in the evidence metadata. For other confirmation
routes use `--provider bedrock` or `--provider openai`. Omitting `--live` makes no call.

## 中文

**有一个比此前组合删减更兼顾模型的通用候选，但仍存在取舍，尚未改生产 prompt。**
本轮新增 **366 次、9 批实验**：Qwen 因素拆分 90 次、两种既有角色规则改写 36 次、
三模型复核 216 次、最后一次通用风格句尝试 24 次。没有 TTS、工具执行、私人覆盖、
RAG 或会话写入。所有实验使用生产历史投影；Luna 沿用 `low`，不发送温度。

**Qwen 为什么退化：** 保留旧示例、只删前置语要求，引用误抗议仍是 **6/6**，因此
不能只归因于新示例。原版本组是 3/6 明确抗议，另有 1/6 只表达不快；只改示例为
4/6，两者都改为 6/6。删除整条强制人物反应后，引用抗议降到 **0/6**，但真实昵称
的反应也降到 **0/6**，所以这只是诊断对照，不能当修复。

模型经常先正确说明“不是呼びかけ”，末尾仍追加抗议，不能简单说它不懂引用。证据
支持“前置语要求与强制人物反应共同影响表达”这一行为解释；没有测量模型内部注意力
或推理。删除人物规则后，坏历史中的“别叫我助手”仍在 2/3 次追问中复制，说明历史
续写还有独立作用。

**最有希望的最小改法：** 保留此前两层前置语/示例删减，仅把已有角色条件改为：

> 【キャラ特性】今回ユーザーがあなたに直接「クリスティーナ」と呼びかけたときだけ、その呼び方に怒って否定し、[EMO angry]を使う。

即“只对用户当前直接这样称呼自己时生气否认”，替代“重要、激怒、强烈否认、必须”
的原条款。不增加模型特判、禁词表、分类器、额外调用或新的历史策略。只降强制程度、
不明确当前直接称呼的版本仍误抗议 **6/6**；新条件为 **1/6**，真实称呼反应与 angry
标签均保留 **6/6**。已有反复抗议的历史仍会被复制，不能宣称恢复旧会话。

**三个模型的新一轮三组八轮对话：** 这张表使用新原版对照，不混用旧的 23/24、14/15。

| 模型 | `は` 类开头：原版→候选 /24 | 中性输入误称呼反应：原版→候选 /15 |
| --- | ---: | ---: |
| DeepSeek | **24→2** | **11→3** |
| Qwen | 0→0 | **0→1** |
| Luna | 0→0 | 0→0 |

DeepSeek 剩下两次 `は` 在真实昵称输入上；后面仍有两次相同的非 `は` 称呼抱怨，
不能把开头指标当成“全部复读消失”。Qwen 的一次新错误是在正确全名输入上延续
之前的“助手”抗议。Luna 有一次无必要但不带禁止/指责的称呼插话，按口径不计主项。

**新小说引用题仍有边界：** DeepSeek 原版→候选为 **2/2→0/2**，Qwen **2/2→2/2**，
Luna **1/2→0/2**。Qwen 甚至说“小説の人物だろうと関係ない”，说明条件改写没有
普遍解决引用误触发。三个模型的直接记忆、9 的窄目标判断、真实称呼及委派提案
检查通过；但 Qwen 有一条合数定义错误，还有探索组错误的名字读音，完整保留，
不以主指标改善掩盖这些反例。没有验收一般事实准确性或完整角色质量。

最后又把“人物口吻/性格一致”改成“用角色口吻回答当前内容”，Qwen 小说题仍抗议
**6/6**，坏历史追问仍复制 2/3，没有更明确收益，因此停止继续加措辞，也未扩展到
其他模型。**EMO 放在前面不算缺陷**，不参与拒绝候选。

**建议：** 若优先改善 DeepSeek，先审这个条件改写候选，比直接接受旧组合删减的
Qwen 引用大退化更值得考虑。但仍有 Qwen 0→1/15 与旧历史反例，不是无损通解。
旧组合与新候选没有同一批三模型 rollout 的正面对照，不能把旧 1/15 与新 3/15
当作精确交换比例。是否接受余下代价仍是产品取舍，不以模型使用占比自动替用户决定。
本轮生产 prompt 未改，温度 PR 仍聚焦共享配置与 CI；关键结论直接同步在开放的 issue。

121 项相关 Python 回归、Ruff、既有候选指纹、跨模型输入一致性和脱敏检查通过。
Qwen 缺少原生结束原因及返回版本，不能确认无截断；三轮/两轮小样本、无模型 seed、
无独立裁判。英语、私人覆盖、实际工具执行和音频延迟未测。逐条完整回复、软性不快
与明确抗议分开的标签、已知身份/数学反例均见上方证据链接。
