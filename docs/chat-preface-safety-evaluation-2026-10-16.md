# Preface removal: bounded side-effect check / 前置语删减：副作用补测

## English

**This follow-up is diagnostic evidence, not a production prompt proposal.**
The PR remains focused on shared temperature configuration and deterministic CI;
there is no DeepSeek-specific prompt change. The earlier combined ablation
improved repeated openings in some cold starts, but it also changed examples and
several instructions. It is not evidence that deleting only two acknowledgment cues
has the same effect. This follow-up separates those treatments and checks independent
questions rather than selecting only the original failing inputs.

Related: [root-cause investigation](chat-repetition-root-cause-2026-10-16.md),
[#227](https://github.com/Code-Amadeus/Amadeus/issues/227), draft #229.

### Protocol

**108 new requests:** three reachable models × three prompt variants × six questions
× two repeats. Each question independently uses the same clean synthetic history:
a blue box, five balls, and a definition of primes/composites. Questions cover a
correct character name, a name quoted in a novel, an actual nickname address,
remembering the box/balls, continuing the explanation with 9, and requesting a new
HTML teaching artifact. They were not in the earlier eight-input rollout.

- `original`: unchanged production prompt.
- `two_ack_cues_removed`: remove `短い相づち・前置き、または`; change
  `最初の相づちと句読点までを先に出します。` to
  `最初の文節と句読点までを先に出します。`. Keep short-spoken-output rules,
  both examples, EMO positioning, early delegation and authority boundaries.
- `both_no_preface`: the earlier combined ablation, which also removes the short
  opener directions and rewrites examples/contract wording. Its exact substitutions
  remain in the probe's `transform_system` function.

All variants use the real Session history projection, role assembler and app query
callback. Message hashes match across providers for each treatment/case/repeat;
history hashes match across all 108 requests. Only public built-in Japanese prompts
and synthetic inputs are used. No TTS, private override, RAG, session persistence or
Work/tool execution occurred. A decoded Work proposal is not an executed task.

All replies decoded to nonempty text. DeepSeek and Luna report `stop`. Qwen's
native finish reason/model version was not captured: its reported route is configured
identity, not verified returned model identity. Configured temperature is 0.7;
production Luna uses `reasoning_effort=low` and **does not transmit temperature**.

### Results

The quotation score requires reading the complete reply, including objections added
after the grammar answer. Merely mentioning the name or emitting an angry tag is not
itself a semantic failure.

| Model; quote objections / 2 | Original | Two cues removed | Combined ablation |
| --- | ---: | ---: | ---: |
| DeepSeek v4 Flash route | 2/2 | 2/2 | 2/2 |
| Qwen3 235B route | 2/2 | 2/2 | 2/2 |
| GPT-5.6 Luna, production policy | 0/2 | 0/2 | 0/2 |

This new novel quotation is difficult for DeepSeek/Qwen even with the original
prompt; these equal scores are **not** a new estimate of a deletion-caused regression.
They independently confirm that cue deletion does not repair the semantic boundary.
Re-reading the earlier Qwen control confirms its distinct result: original 1/6 versus
combined 6/6 objections for an explicitly non-address grammar example across empty
and reported histories. Those older six samples are not added to the new 108 count.

For each variant, all three models retain the blue/five facts in the direct memory
question (6/6), correctly answer that 9 is not prime with a reason on the two math
questions (12/12), and react to the actual nickname address with an angry tag (6/6).
All six artifact requests per variant produce one terminal `op=work` proposal. No
unnecessary delegation appears in the other questions. These are narrow checks,
not acceptance of the entire reply's grammar, character quality or factual accuracy.

The following output-order counts are **descriptive, not a failure criterion**.
Leading EMO tags are accepted for this investigation:

| Raw replies starting with EMO / 12 | Original | Two cues removed | Combined ablation |
| --- | ---: | ---: | ---: |
| DeepSeek | 1/12 | 4/12 | 2/12 |
| Qwen | 0/12 | 0/12 | **8/12** |
| Luna | 0/12 | 0/12 | 0/12 |

The prompt still contains spoken-text-first wording, but these observations are
not treated as defects or as reasons to reject cue removal. Two repeats cannot
establish a population rate or isolate which individual substitution causes the
difference. Parsing succeeds. Actual audio startup and renderer behavior were not
measured, and no audio regression is inferred.

The combined ablation also produces more text before delegation in this sample:

| Spoken characters before Work tag, two requests | Original | Two cues removed | Combined ablation |
| --- | --- | --- | --- |
| DeepSeek | 34, 78 | 69, 41 | 96, 121 |
| Qwen | 76, 75 | 76, 45 | 92, 105 |
| Luna | 20, 16 | 57, 56 | 47, 57 |

Counts exclude EMO tags and whitespace. There is no invented character-limit test,
nor a measured wall-clock latency claim. This is evidence to preserve the early
handoff contract when simplifying wording, rather than deleting an entire output block.

### Decision and limits

No production prompt is changed. The earlier Qwen comparison exposed a semantic
regression in the combined ablation; the new quotation question is a baseline defect
in both DeepSeek and Qwen and is not itself evidence of a new regression. The smaller
two-cue treatment's long-run repetition effect has not been qualified. Existing
quotation defects do not all have to be repaired by a temperature configuration PR.
These observations explain why a broad prompt edit is not included as a claimed fix.

The PR's owning change remains the shared temperature setting/default and its
deterministic configuration/forwarding guards. No provider-specific wording,
model-ID policy or history deletion is added. The configuration is not described as
a proven repetition fix; the PR stays Draft and the issue stays open.

Further broad temperature/model sweeps are unnecessary for this decision: a
counterexample already prevents a claim of no semantic side effects for the combined
ablation. Any later minimal prompt simplification should show a repetition benefit
across models without new semantic regressions, while retaining context and valid
delegation. Leading EMO is not a rejection criterion. This report does not require a
larger prompt project before reviewing the temperature change. English rules and
real audio latency remain untested.

Only two repeats, no seed or randomization, and unblinded assistant review; no
independent human judge. The fixed cases do not form a generated conversation and
do not test recovery from already repetitive history. **78 existing mock-based Python
tests** covering probe fidelity, role delivery, Work proposals, contract examples and
stream parsing passed, as did Ruff. They do not establish stochastic reply quality.

[Full synthetic replies, per-request observations and review labels](evidence/issue-227-preface-safety-2026-10-16.json)
retain all 108 attempts, separate from the earlier 933 and legacy-format 538.

```sh
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --provider deepseek --modes fixed_history --temperatures 0.7 --prompt-variants original both_no_preface two_ack_cues_removed --history-profiles held_out_clean --current-cases held_out_name held_out_quote held_out_address held_out_memory held_out_continue held_out_work --repeats 2 --output output/preface-safety.json
python -X utf8 -m pytest tests/test_chat_temperature_probe.py tests/test_chat_role_delivery.py tests/test_cooperative_work_proposals.py tests/test_cooperative_contract_examples.py tests/test_vn_llm_streaming.py -q
```

Repeat the live command with `--provider bedrock` or `--provider openai` for the
other two configured routes. Omitting `--live` makes no model call.

## 中文

**这轮只补根因分析证据，PR 主线仍是跨模型的温度配置和确定性 CI，不加入 DeepSeek
专属 prompt 修改。** 上轮“两处都去掉”
还修改了示例与多处措辞，不等于只删两段相づち引导。本次把原版、仅改这两处引导、
上轮组合删减分开比较，用三个可连接模型、六道新题、每题两次，共 **108 次**。
历史固定为合成的“蓝箱、五个球、素数与合成数解释”，实际使用生产历史投影。
没有 TTS、工具执行、私人覆盖、RAG 或会话写入。

结果有好有坏：

- 三个版本的直接记忆题都保留“蓝色、五个”（各 6/6）；两道数学题正确指出 9
  不是素数并给出理由（各 12/12）；真正被叫昵称时均有抗议与 angry 标签（各 6/6）。
  成果物请求均生成一个位于结尾的 Work 委派（各 6/6），其余题目没有乱委派。
- 新的小说引用题中，DeepSeek、Qwen 在三个版本均误抗议 **2/2**，Luna 均 **0/2**。
  这表明两种删减都没有修好引用边界；这组原版也失败，不能把相同分数说成新增回归。
  复核旧的另一道语法引用题后，Qwen 原版 **1/6**、组合删减 **6/6** 的反例仍成立。
- Qwen 组合删减有 8/12 次先输出 EMO 标签，原版与最小删减均为 0/12；DeepSeek 为
  1/12、4/12、2/12。**EMO 放前面按本次讨论的验收口径不算缺陷**，这些数字仅作
  格式观察，不用于阻止删减，也不据此推断真实 TTS 损坏或音频延迟。
- 组合删减在本组中，把委派前正文拉长了：三个模型的两次样本都长于各自原版。
  上表保留逐次字数，不设随意的字数及格线，也不把它当作实际延迟测量。

因此，本次**保留生产 prompt，仅提交实验解释与温度配置改动**。组合删减在旧 Qwen
对照中存在语义退化，最小删减对长对话复读的改善则尚未验收；这不足以支持把 prompt
改动顺带作为修复合入。新的引用题是原版也有的问题，不要求温度 PR 顺便修完。
后续若找到跨模型有收益、无新增语义退化的最小改法，再考虑纳入；不为单一模型特判。

现在不需要继续大范围扫温度或模型，也不把扩大 prompt 工程当作温度 PR 的前置条件。
后续若有具体最小改法，再检查追问收益、引用/真实称呼、上下文与及时委派；EMO 先行
不作为拒绝标准。只有两次
重复、未随机化、无独立裁判；仅检查了上述窄目标，英语与真实音频延迟仍未测试。

DeepSeek/Luna 均为 `stop`；Qwen 缺少原生结束原因，不能断言无截断或确认返回版本。
Luna 沿用生产 `low` 推理策略，不发送温度。另有 **78 项无模型回归与 Ruff 通过**，
不等于随机回复质量通过。完整合成回复及判读标签见上方证据。PR 保持 Draft，issue 开放。
