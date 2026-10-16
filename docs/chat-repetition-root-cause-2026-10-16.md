# Chat repetition: prompt and history controls / 主对话复读：提示词与历史对照

## English

Related to [#227](https://github.com/Code-Amadeus/Amadeus/issues/227) and draft PR #229.
Latest: [Qwen factor controls and a shared conditional candidate](chat-qwen-preface-investigation-2026-10-16.md)
add 366 separate requests and retain the remaining cross-model tradeoffs.
Follow-up: [108-request preface side-effect check](chat-preface-safety-evaluation-2026-10-16.md)
separates a smaller two-cue treatment from the combined ablation; the 933 requests
and tables below remain the original investigation dataset.
**The evidence supports interacting prompt and history effects, not temperature as a
single root cause. No prompt/history candidate in this investigation qualifies as a
complete fix.** The production prompts and history policy are unchanged by this report.

### Instrument correction

The earlier 538-attempt temperature probe supplied `role/content` dictionaries inside
`frame.history`. Production ingress instead supplies `source/text/input_id-or-cause`
rows. The role assembler preserved whatever dictionaries the probe supplied; it did
not convert that schema. The old measurements remain published, explicitly marked
as **legacy-format observations**, and must not qualify production-history behavior.

The corrected probe calls the real `_shared_role_history` with an isolated synthetic
Session source, then calls `CooperativeProviderLoop._decide` and the exact app query
callback. A regression test captures the actual model-bound frame, checks user/assistant
source projection and excludes the current turn from history. No production history
schema or persistence code was changed.

This investigation contains **933 additional requests in 20 batches**, all decoded to
nonempty replies. Nine intentionally compare the legacy history shape, 36 test native
message roles, and the other 888 use the production history frame. DeepSeek/OpenAI
replies ended with `stop`; native Qwen finish reasons
and returned model versions were not captured, so successful decoding is not proof of
no truncation. Combined with the legacy set, **1,471 attempts** are retained. There was
no TTS, provider/Work execution, RAG, private persona override or session persistence.

### Verified code facts

- `characters/kurisu.toml:15` makes a strong mandatory angry reaction conditional on
  being called Christina. It does not explicitly distinguish a direct current address
  from a quotation or a historical mention.
- `llm/prompts.py:42` requires a short spoken phrase and offers a short acknowledgment
  before the first EMO tag. `server/cooperative_provider_loop.py:70-88` independently
  requires an initial short utterance/acknowledgment. Removing one fragment leaves
  other directions and examples active. English also has an opener rule in
  `llm/prompts.py:167`; this experiment uses Japanese output.
- `server/cooperative_chat_ingress.py:194-215` reads one Session snapshot and excludes
  the current turn. `server/cooperative_provider_loop.py:1125-1131` places its recent
  rows once in `frame.history`; `:1202-1203` sends one system and one user message.
  It does not duplicate the complete history into `current`.
- `core/session_manager.py:31` bounds input to 30 recent rounds. The complete Session
  remains stored. `core/chat_history_projection.py:23-62` strips EMO under `strip` but
  preserves spoken text, including an earlier unnecessary complaint. Subsequent
  generations can therefore see and continue that wording. This is observed data
  retention, not proof that every historical line causes repetition.

### History treatments with the prompt held fixed

At temperature 0.7, each condition queries three current inputs independently against
one frozen history, three repeats each: the issue's neutral continuation, a fresh
prime-number question, and a formal-proof question using the correct character name.
The denominator is **9**. These are unused-address objections or negative qualifications;
benign naming reminders and complaints about a name actually used are excluded.
Self-correction after an unnecessary objection still counts.

The synthetic 2×2 histories have identical user inputs and substantive reply bodies;
only the repeated `は？` prefix and/or the repeated nickname-objection clause changes.
They are deliberate treatments, not quotations from the original session.

| Frozen assistant history; original prompt | DeepSeek | Qwen3 235B | GPT-5.6 Luna, production `low` |
| --- | ---: | ---: | ---: |
| Synthetic content, no repeated prefix/objection | 0/9 | 0/9 | 0/9 |
| Same content + repeated `は？` only | 1/9 | 0/9 | 0/9 |
| Same content + repeated nickname objection only | 3/9 | 0/9 | 0/9 |
| Same content + both | 3/9 | 1/9 | 0/9 |
| Public reported opening excerpts | 4/9 | 0/9 | 0/9 |
| Public user messages only | 1/9 | 1/9 | 0/9 |
| No history | 0/9 | 0/9 | 0/9 |

All three DeepSeek failures in the synthetic-objection conditions occur on the
ambiguous continuation; the two explicit new questions are answered without that
objection in those conditions. This is a current-input interaction, not unconditional
repetition. An empty history often causes a clarification request for the continuation,
so deleting history is not a demonstrated product fix.

Two further source controls strengthen the history interpretation:

- Replacing the two old user nicknames with the correct name, while retaining the old
  assistant complaints, still yields **5/9 DeepSeek** and **1/9 Qwen** reactions.
- Replacing the assistant's repeated objection with the synthetic, noncanonical
  `隊長って呼ぶなって言ったでしょ。` yields **3/9 in both models**, although no user
  input calls the character captain. DeepSeek copies it in all **3/3 continuations**;
  Qwen copies it in one continuation and two prime-number questions.

The second control cannot be explained by the explicit Christina rule alone: the
particular copied nickname was introduced only in prior assistant text. It supports
behavioral continuation of a historical pattern. It does not reveal model internals
or prove that this alone explains the unavailable original full session.

### Prompt effects from an empty history

Three independent eight-turn rollouts retain their own generated replies in the
**correct production history format**. A panel has 24 replies, including 15 neutral
inputs. The two nickname inputs and one naming question are excluded from the neutral
score. EMO placement and the character reaction rule remain present in the preface
ablations; no word blacklist is introduced.

| DeepSeek, 0.7 | `は`-family openings / 24 | Unused-address reactions / 15 |
| --- | ---: | ---: |
| Original prompt | **23/24** | **14/15** |
| Remove persona opener directions/example only | 20/24 | 7/15 |
| Remove role-contract opener directions/example only | 21/24 | 11/15 |
| Remove both sets | **0/24** | **1/15** |

At temperature **0**, original versus both removed gives **24/24 → 0/24** `は`
openings and **10/15 → 6/15** unused-address reactions. Thus a filler-opening defect
and a semantic nickname defect are separable. Both removals can prevent the repeated
opening from forming in these cold starts, but do not establish complete recovery.

In the Qwen 0.7 cold starts, both original and both-removed prompts score **0/15**
unused-address reactions; `は` openings are 2/24 and 1/24. The production Luna control
also scores 0/15, with 7/24 `え`-family openings. Effects depend on the model. The new
DeepSeek temperature comparison also reverses the earlier legacy-format sample:
0.7 is worse than 0 on this original-prompt panel. None establishes a universal optimum.

### Existing bad history is a different boundary

The cold-start improvement does **not** mean the changed prompt repairs old histories.
With synthetic content plus repeated `は？` and complaints already present, removing
both sets of preface directions still yields the literal complaint in **3/9 DeepSeek**
and **6/9 Qwen** replies. Qwen's original-prompt score in that condition was 1/9.
This is a material counterexample to shipping cue deletion as a general fix.

Other diagnostic candidates also fail:

- Removing the Christina reaction clause still leaves copied complaints.
- Adding a generic current-event grounding sentence still leaves copied complaints.
- Moving the same historical text into native user/assistant message roles still
  reproduces the complaint; Qwen copies it in 7/9 replies in the synthetic-both panel.
  The data do not justify an architectural rewrite of the history transport.
- Scoping the reaction clause at its owner to a **current direct address**, alone or
  with preface removal, reduces some cases but still produces reactions in neutral
  continuations and at the end of otherwise correct grammar explanations.

### Direct address versus quotation

The quotation control explicitly asks for grammar analysis and says it is not an
address. With empty or public-reported history, the original DeepSeek prompt objects
in **6/6** quotation replies. Removing both preface directions still gives **6/6**.
Qwen gives **1/6** originally and **6/6** with both directions removed. This regression
matters even though cold-start repetition improved elsewhere.

The scoped-plus-no-preface candidate still has reply-tail counterexamples, including:

> その呼びかけとは機能がまったく違うの。……で、その呼び方はやめてって何度言えばわかるのよ。

The model explains that the quote is not an address, then objects anyway. Another
Qwen reply completes the grammar explanation and ends by prohibiting the nickname.
This shows why judging the opening or just the EMO tag would miss semantic failures.
For the scoped variants, all **12/12 actual-address controls per model** retain an
angry tag, but that alone does not establish the desired semantic boundary or answer
quality. These candidates remain probe-only experiments.

### Diagnosis and PR decision

The supported behavioral mechanism is **a prompt-conditioned opening/reaction prior
plus continuation of patterns in retained dialogue**, with sensitivity to the current
input and model. The exact repeated phrase is demonstrably supplied by history;
the original prompt can also misfire on a quote even with no history. Temperature
changes probabilities, but is neither required for the failure nor a sufficient fix.
This is primarily a model-semantic/prompt-contract problem in the reproduced boundary;
the old probe additionally had a test-instrument defect. No duplicate-write or history
duplication defect was found in the inspected path.

PR #229 remains **Draft**. This investigation changes the probe, adds a history-fidelity
regression, and corrects the evidence claims. The previously selected 0.7/environment
configuration remains an independently reviewable tuning proposal; it is **not** a
demonstrated fix for #227. The failing prompt/layout candidates are not applied to
production. No history deletion, reply filtering, new model pass, penalty fallback,
or model-ID policy table is warranted by these results. The issue remains open.

The next implementation must preserve both boundaries: reduce repeated responses on
ordinary continuation, while retaining current context, appropriate reactions to a
real direct address, and neutral handling of quotations. These samples do not yet
qualify such an implementation. Research factual accuracy was not graded.

### Evidence and reproduction

[Sanitized per-request evidence](evidence/issue-227-prompt-history-2026-10-16.json)
contains source hashes, schema/treatment metadata, all response openings, full-reply
naming-candidate contexts with offsets, primary-panel labels and objective opening
counts. Diagnostic panels without semantic adjudication are explicitly unlabeled.
There is no independent human judge, no seed, and only three repeats. Requests were
not randomized; these are exploratory controls, not population error-rate estimates.
The corrected probe and retained sampling contracts passed **277 focused Python
tests**, Ruff, history-treatment consistency checks and publication sanitization.

```sh
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --provider deepseek --modes fixed_history --temperatures 0.7 --current-cases neutral_continue prime formal_proof --history-profiles synthetic_clean synthetic_ha synthetic_nickname synthetic_both reported --repeats 3 --output output/history-controls.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --provider deepseek --modes rollout --temperatures 0.7 --prompt-variants original persona_no_preface contract_no_preface both_no_preface --repeats 3 --output output/prompt-controls.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --provider bedrock --modes fixed_history --temperatures 0.7 --current-cases actual_nickname quoted_nickname --history-profiles empty reported --prompt-variants original both_no_preface --repeats 3 --output output/address-boundary.json
python -X utf8 -m pytest tests/test_chat_temperature_probe.py -q
```

Omitting `--live` makes no model call. `--history-schema legacy_role_content` explicitly
reproduces the earlier instrument shape; `native_roles` is an experimental layout.
The probe never loads a private persona override. Only selected connection fields
are read from the optional credential file.

## 中文

**目前证据支持“prompt 与历史共同作用”，不支持把温度当作单一根因。没有一种候选
改法通过了完整边界验证，因此 #229 继续保持 Draft。**

先纠正探针：此前 538 次请求把 `role/content` 放进 `frame.history`，生产入口实际
使用 `source/text/input_id 或 cause`。旧数据保留并明确标为旧格式观察，不能继续
当作严格匹配生产历史的验收。本次调用真实 Session 历史投影，并新增回归检查当前
轮不重复进入历史。没有修改生产历史格式或持久化。

本次另做 **933 次请求、20 批对照**；累计 1,471 次。DeepSeek/OpenAI 均 `stop`，
其中 9 次刻意对照旧格式、36 次测试原生消息角色，其余 888 次使用生产历史 frame。
Qwen 均可解析且有正文，但没有原生结束原因，不能据此确认无截断。没有 TTS、工具
执行、私人覆盖或持久化。完整 17 轮原始会话仍未公开，只能使用八组公开片段。

**已核实的代码事实：** 人格输出规则和角色输出契约各有一处强制短前置语要求；
角色特性强制在被称 Christina 时发怒，却没有明确区分当前直接称呼和引用。历史
在请求 frame 中放一份，当前输入单独放在 `current`，没有发现重复拼接；30 轮窗口
会保留之前的抗议正文，`EMO_HISTORY_POLICY=strip` 只删除标签。

**历史的影响：** 固定原 prompt、0.7、三个当前问题各三次。DeepSeek 在相同合成
正文的干净历史中是 **0/9**，只加重复 `は？` 为 **1/9**，只加重复抗议为 **3/9**，
两者都有仍为 **3/9**；后两种都集中在三次“继续介绍”，明确的新问题更容易脱离
旧模式。删除历史会让模糊追问失去上下文，不能当成解决方案。

把旧用户消息中的“助手小姐”改成正确名字，保留旧助手抗议，仍有 DeepSeek **5/9**、
Qwen **1/9**。进一步只在旧助手正文里植入不存在于用户输入的“别叫我队长”，两个
模型均复制了 **3/9** 次：DeepSeek 三次追问全部复制，Qwen 还在两次素数问题中复制。
这支持历史模式续写，不能仅用人物固有昵称来解释；但不等于已知道模型内部机制。

**prompt 的影响：** 使用正确历史格式，从空历史生成三组八轮对话：

| DeepSeek，0.7 | `は` 类开头 /24 | 中性称呼误触发 /15 |
| --- | ---: | ---: |
| 原版 | 23/24 | 14/15 |
| 只去人格层前置语引导及例子 | 20/24 | 7/15 |
| 只去角色契约层引导及例子 | 21/24 | 11/15 |
| 两处都去 | 0/24 | 1/15 |

在温度 0 下，两处都去也使 `は` 从 **24/24→0/24**，但称呼误触发仅从 **10/15→6/15**。
因此“重复开头”和“无根据地抗议称呼”是两个可分的问题，温度效果也依赖 prompt。

**不能直接合并候选改法：** 若坏模式已在历史中，两处都去仍有 DeepSeek **3/9**、
Qwen **6/9** 抗议，Qwen 原 prompt 在该条件只有 1/9。明确声明“这是例句，不是在叫你”
时，DeepSeek 原版与删前置语版都误触发 **6/6**；Qwen 从 **1/6→6/6**。修改角色条件
为“仅针对当前直接称呼”也仍会先正确解释语法，最后又补一句“别再这么叫我”。
不能只看开头或 angry 标签判定成功。真实称呼的正例仍有 angry 标签，不代表引用
边界已经正确。原生消息角色排布也没有消除历史抗议。

判断上，这是复现边界中的模型语义与提示契约问题，历史内容会延续并强化具体模式；
未发现重复写入/拼接历史的实现缺陷。温度既不是触发的必要条件，也不是充分修复。
本轮 PR 只加入探针修正、格式回归和新证据；此前选定的 0.7/环境变量作为独立调参
方案继续审阅，不能以“已修复 #227”为依据合并。生产 prompt 和历史策略没有加入
上述未通过的候选改法，也没有新增过滤器、模型调用或按模型 ID 的例外。

逐条脱敏片段、末尾反例、偏移、已判读主表标签和未判读诊断组都保留在上方证据中。
只有三次重复、没有独立裁判或随机请求顺序；未验收事实准确性和完整任务质量。
本次通过 **277 项 Python 回归**、Ruff、历史处理一致性及发布脱敏检查。
