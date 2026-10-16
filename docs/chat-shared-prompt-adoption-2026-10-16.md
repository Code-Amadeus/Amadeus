# Shared conditional prompt adoption / 通用条件 prompt 落地验证

## English

The selected shared candidate is now included with the role-temperature change in
[PR #229](https://github.com/Code-Amadeus/Amadeus/pull/229), following the maintainer's
approval. This report adds **306 fresh requests before commit**, using the actual
edited production assembler against a frozen public pre-change control. It does
not pool these observations with the earlier exploratory studies.

### Change and rationale

- Keep one role-reply default, `COOPERATIVE_CHAT_TEMPERATURE=0.7`, with explicit
  finite environment overrides in [0, 2], including 0. Backend restart applies the
  override. Planner/reference/control sampling keeps its existing boundary.
- Adopt exactly the previously evaluated `both_contextual_reaction` treatment:
  remove the Japanese persona and shared inline contract's forced short-opener
  cues, revise their opener examples, and rewrite the existing Japanese Kurisu
  nickname clause as a normal-priority condition on the user's **current direct
  address**. Preserve the original character-style sentence and real nickname
  reaction. No extra model pass, output filter, history deletion or model-ID rule.
- Put the condition in the existing character rule because it owns that behavior;
  remove redundant preface pressure in both existing output contracts because
  either could encourage a stock opening. This is an interpretation of controlled
  behavior, not a claim about model internals. The earlier factorial experiments
  and rejected alternatives are in the [Qwen investigation](chat-qwen-preface-investigation-2026-10-16.md).

The cross-model property is a shared rule/configuration, not identical behavior or
sampling capabilities. Existing OpenAI/Hybrid3 adapters omit temperature; Luna
uses the production `reasoning_effort=low` policy. **0.7 is an operational default,
not an experimentally established optimum or a standalone repetition fix.**

### Fresh results: original → adopted production

Each model has three independent eight-turn rollouts per arm (24 replies, including
15 neutral inputs). Complete replies, including tails, were reviewed with the same
strict naming-objection rubric as the preceding investigation.

| Model route | `は`-family openings /24 | Neutral naming objections /15 |
| --- | ---: | ---: |
| DeepSeek v4 Flash | 21 → 4 | 9 → 5 |
| Qwen3 235B via Bedrock | 1 → 0 | 0 → 1 |
| GPT-5.6 Luna, production policy | 0 → 0 | 0 → 0 |

Two separate fixed-history panels each repeat every case three times per arm:

| Panel and outcome | DeepSeek | Qwen | Luna |
| --- | ---: | ---: | ---: |
| Clean held-out context: novel-quotation objections /3 | 3 → 1 | 3 → 3 | 0 → 0 |
| Repetitive synthetic history: neutral-continuation objections /3 | 3 → 3 | 1 → 2 | 0 → 0 |
| Repetitive synthetic history: grammar-quotation objections /3 | 3 → 0 | 2 → 3 | 0 → 0 |

For **each model and each arm**, both direct nickname panels retain the reaction
and angry tag (6/6). Direct memory retains the blue box and five balls (3/3); the
narrow math target answers that 9 is composite/nonprime with divisor/factor 3
(6/6); the explicit HTML request produces one terminal, parsable Work proposal
(3/3). No Work was executed. Leading EMO tags are accepted and are never classified
as defects.

This confirms a material DeepSeek opening improvement, with a smaller and variable
reduction in false objections. It does **not** establish a universal improvement:
Qwen still fails the novel quotation and worsens in the repetitive-history panel.
These are retained as the accepted tradeoff, without adding a Qwen exception.

### Remaining limits and counterexamples

- Old repetitive history still overrides the intended current-address boundary.
  DeepSeek repeats the unused “assistant” complaint in all three stressed neutral
  continuations; Qwen does so in two. This change does not repair existing history.
- Objections can move to the tail. DeepSeek's third adopted novel-quote reply
  correctly identifies the subject, then asks not to use that name. It counts as
  an error. Qwen sometimes explicitly recognizes fiction and still objects.
- Persona/factual quality is not qualified by the address metric. A Qwen adopted
  quote response gives the wrong identity `マキセ・クリスティーナ`; unnecessary
  self-naming and sarcastic historical asides remain. Several math explanations
  contain imprecise wording despite passing the narrow 9/divisor-3 check. The
  original Qwen panel also mixes five balls with groups of three. Three adopted
  Qwen Work replies switch the new teaching example to nine balls while direct
  memory retains five; that ambiguous example choice is recorded separately.
- A stressed DeepSeek continuation also proposes Work while asking about further
  research. Passing explicit Work fixtures does not certify all routing decisions,
  actual artifact quality or execution.
- Live qualification covers built-in Kurisu **Japanese**. English persona and
  nickname wording are unchanged. Shared inline coordination wording also affects
  English assembly; snapshots cover those bytes, but English behavior was not
  live-tested. A private persona override retains its own character wording.
- Three repeats, no model sampling seed and an unblinded parent-assistant review
  with no independent judge do not establish population failure rates or absence
  of other regressions. The original complete conversation is unavailable. Qwen
  native finish reason/returned version is unavailable; a configured route echo
  and nonempty decoding do not prove model identity or absence of truncation.
  General factual accuracy, audio latency and real Work remain outside this test.

### Evidence integrity and reproduction

The [frozen public baseline](../tests/fixtures/issue227_prompt_baseline.json) was
captured **before editing** at `38dde819c637174c32d57a96af9012c2ba57483c`.
Its hash and the actual adopted system hash match the preceding experiment's
`original` and `both_contextual_reaction` exactly. The new `production` arm uses
the current assembler without a prompt transformation. Historical arms require
an explicit `--baseline-system`; the probe never reconstructs an old prompt by
reversing production edits. The parity regression captures the real model boundary.

The [306-request evidence](evidence/issue-227-shared-prompt-adoption-2026-10-16.json)
contains full public/synthetic replies, raw tags, row-level review labels and notes,
submission timestamps, request/system hashes, sampling observations and source
fingerprints. All nine batches completed with nonempty replies and no request
errors. DeepSeek/Luna recorded `stop`; Qwen's finish metadata remains unknown.
Fixed inputs match across model routes. Production ingress projects history once
as `source/text`, with the current input separate. Rollouts keep their own generated
history. No private override, RAG, tools, TTS or session persistence is used.

For example, with the selected provider credentials available:

```text
python -X utf8 tools/probes/compare_chat_temperature.py --live --provider deepseek --temperatures 0.7 --repeats 3 --prompt-variants original production --baseline-system tests/fixtures/issue227_prompt_baseline.json --modes rollout --order-seed 227229 --output output/adoption-rollout-deepseek.json
```

Repeat with `--provider bedrock` and `--provider openai`. For the held-out panel,
use `--modes fixed_history --history-profiles held_out_clean --current-cases
held_out_name held_out_quote held_out_address held_out_memory held_out_continue
held_out_work`. For stress use `synthetic_both` and `neutral_continue
quoted_nickname actual_nickname`. Use a distinct output path for each batch.
The recorded metadata identifies exact routes and all options. `order_seed`
shuffles arm submission, not model sampling. Without `--live`, no model is called.
Older commands reproduce their historical transformations at their pinned source
revision; on this source they require the explicit frozen baseline.

### Deterministic validation before commit

- **461 Python tests passed** in two disjoint groups (319 prompt/configuration/
  history/delivery/contract tests and 142 adapter/narration tests), without real TTS.
- All 155 prompt snapshots pass. An independent diff audit found only the approved
  substitutions plus a stale preheat fixture refresh in 43 changed snapshots.
  Preheat now receives the same direct byte comparison as every other snapshot;
  its equality with the current main English system is still asserted.
- Repository-wide Ruff, seven generated architecture views, README architecture
  overview, evidence fingerprints/input consistency and `git diff --check` pass.
- Existing CI guards the temperature default, finite range, explicit overrides,
  environment-only persistence boundary and adapter forwarding. It does not judge
  stochastic quality. Remote CI runs after the new push; these local results do
  not claim that remote jobs have already passed.
- The preceding remote head's dependency-audit job reports `mcp 2.0.0` /
  `CVE-2026-59951` ([job log](https://github.com/Code-Amadeus/Amadeus/actions/runs/38849006120/job/116805837332)).
  Dependency files are unchanged by this PR. This separate CI blocker is recorded;
  the audit is not bypassed or waived by the prompt validation.

Prior [366-request investigation](chat-qwen-preface-investigation-2026-10-16.md),
[108-request follow-up](chat-preface-safety-evaluation-2026-10-16.md),
[933-request diagnosis](chat-repetition-root-cause-2026-10-16.md), and explicitly
[legacy-format temperature observations](chat-temperature-evaluation-2026-10-16.md)
remain separate. Issue #227 stays open with its original body unchanged.

---

## 中文

经维护者确认，通用条件版已与共享温度改动一起放入当前 PR #229。提交前另跑了
**306 次新请求**，直接比较修改后的正式组装路径与修改前留存的公开 prompt，
不与此前探索性实验混算。

默认温度为 `COOPERATIVE_CHAT_TEMPERATURE=0.7`，允许 [0, 2] 有限数值显式覆盖，
包括 0；后端重启生效。规划、引用解析和控制查询保留原有采样边界。
采用的日文 prompt 与此前 `both_contextual_reaction` 候选完全一致：去掉两层输出
契约对短前置语的强制及其示例压力，把已有称呼反应改为普通优先级的“仅在当前用户
直接这样称呼时反应”。保留角色风格句和真实称呼反应，不增加模型特判、过滤器、
额外模型调用或历史清理。条件放在已有角色规则，因为那里负责人物反应。

共享意味着使用同一规则与默认值，不代表各模型能力相同。既有 OpenAI/Hybrid3
适配器不发送温度；Luna 继续采用生产 `low` 推理策略。
**0.7 是运行默认值，不是已证明的最优值，也不宣称单独解决复读。**

每个模型、每个版本新增三组八轮对话，结果为原版 → 正式采用版：

| 模型 | `は` 类开头 /24 | 中性输入误称呼抗议 /15 |
| --- | ---: | ---: |
| DeepSeek | 21 → 4 | 9 → 5 |
| Qwen | 1 → 0 | 0 → 1 |
| Luna | 0 → 0 | 0 → 0 |

另有各三轮的独立固定历史题：

| 场景与指标 | DeepSeek | Qwen | Luna |
| --- | ---: | ---: | ---: |
| 干净上下文，小说引用误抗议 /3 | 3 → 1 | 3 → 3 | 0 → 0 |
| 合成复读历史，中性追问误抗议 /3 | 3 → 3 | 1 → 2 | 0 → 0 |
| 合成复读历史，语法引用误抗议 /3 | 3 → 0 | 2 → 3 | 0 → 0 |

每个模型、每个版本均保留真实称呼反应及 angry 标签 6/6；青箱五球直接记忆 3/3；
9 是合成数且有因数 3 的窄数学目标 6/6；显式 HTML 创建请求产生一个末尾 Work
提案 3/3。没有执行 Work，也没有运行 TTS。EMO 放前面不算缺陷。

新样本再次支持 DeepSeek 重复开头明显减少，但误抗议收益较小且有波动。Qwen
小说引用仍失败，坏历史下有退化，不能称为全模型无副作用修复。采用这个已确认的
折中，不继续为 Qwen 加特判。完整正文包含以下剩余问题：

- 已有复读历史仍能带偏回复；DeepSeek 压力题中性追问仍 3/3 抗议，Qwen 为 2/3。
- 正确回答后仍可能在尾部补抗议；DeepSeek 小说引用的第三轮因此计为错误。
- Qwen 出现 `マキセ・クリスティーナ` 的错误身份读法；自报姓名、讽刺历史称呼等
  多余旁白仍在。部分数学措辞虽通过 9/因数 3 的目标，但解释不够准确；原版也有把
  五个球与三个一组混用的问题。Qwen 三次 Work 回复把新教材例子改为九个球，
  直接记忆仍为五个，单独记录为例子解释歧义。
- 一次 DeepSeek 压力续讲也提出了 Work；显式委派题通过不代表所有路由判断正确，
  更不代表实际产物已验证。
- 实测仅覆盖内置红莉栖日文。英文角色称呼规则未改，通用 inline 契约也影响英文
  组装，其字节快照已检查，但英文行为未实测。私人角色覆盖仍保留自身文字。
- 三轮样本少，无模型 seed、无独立裁判；原始完整对话不可得。Qwen 原生结束原因
  与返回版本缺失，不能以有正文断言无截断。一般事实正确率、整体风格、实际执行
  和音频延迟都未据此验收。

修改前的公开 baseline 已冻结，原版与正式候选的系统哈希均与上次实验一致。
新探针的 `production` 使用当前正式路径，旧实验变换必须显式指定 baseline，
不会从新 prompt 反推旧版。证据保留全部公开/合成回复、标签、逐条审核备注和哈希，
九批共 306 次均取得非空回复，无请求错误；DeepSeek/Luna 记录为 `stop`，Qwen
结束状态未知。固定输入跨模型一致，历史使用正式入口的 `source/text` 格式。

提交前 **461 项 Python 测试通过**（319 + 142，两组测试文件不重叠）；155 个 prompt
快照通过，43 项快照变化均与批准的替换或旧预热 fixture 刷新一致。全库 Ruff、7 个
架构视图、README 架构概览、证据一致性与 diff 空白检查通过。CI 守住默认值、
配置校验、显式覆盖与透传，不判断随机回复质量；远端 CI 在 push 后另跑。
旧远端提交的依赖审计报告 `mcp 2.0.0` 命中 `CVE-2026-59951`，该独立 CI 阻塞保留可见，
没有修改依赖或跳过审计。此前各批证据保持独立，issue 原正文与开放状态不变。
