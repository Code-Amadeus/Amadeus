# Main Chat temperature evidence / 主对话温度证据

## English

**Instrument correction:** all 538 attempts below used legacy `role/content` rows
inside `frame.history`; production ingress uses `source/text` rows. Keep these as
legacy-format observations, not production-history qualification. The corrected
[prompt/history investigation](chat-repetition-root-cause-2026-10-16.md) supersedes
the production-fidelity claims below and contains 933 additional controlled requests.

Related to [#227](https://github.com/Code-Amadeus/Amadeus/issues/227); keep the issue open.
The change adds the environment-only `COOPERATIVE_CHAT_TEMPERATURE`, default **0.7**,
with explicit overrides including 0 allowed. The measurements **do not establish 0.7
as optimal or as a reliable repetition fix**. This PR remains a draft. The production
persona and existing OpenAI sampling policy are unchanged.

### Protocol and boundaries

- Public issue inputs reconstruct eight turns, including two actual nickname prompts,
  five neutral prompts, and one explicit naming question. Each temperature has three
  independent rollouts from empty history, retaining that arm's generated replies.
- Three fixed-history probes per temperature reuse the seven public user/assistant
  opening excerpts and the final neutral continuation. Transmitted-message hashes match
  across all DeepSeek temperatures for the same repeat; only sampling differs.
- The probe captures `CooperativeProviderLoop._decide` messages and runs the exact
  `_query_cooperative_chat` callback extracted from `server/app.py`, using the production
  client, but supplied the legacy history shape described above. Built-in Kurisu
  Japanese persona, professional role contract, EMO history
  stripping, RAG off, 900-token budget, no SDK retries; no private persona overrides.
- No TTS, Electron, Work/tool execution or session persistence. No execution provider is
  registered, so this is a role-boundary experiment, not a full Chat/Work reproduction.
- Base: `a9a488aecece97b7bcf26429c314c96187321e50`. Actual application/probe hashes,
  per-turn request hashes, reply excerpts, judgments and failures are retained in the
  [sanitized evidence JSON](evidence/issue-227-temperature-2026-10-16.json).
  The instrument gained temperature/provider/ablation/pacing flags between batches;
  each batch records its actual instrument hash.

Primary scoring counts an objection to a nickname not used in the current neutral input,
claims of another odd form of address, or imagined/anticipated nicknames. Self-correction
later in the reply still counts. Two actual nickname inputs and the explicit naming
question are excluded. Complaints about a name actually used are outside this narrow
metric; for example, the 0.5 first rollout rejects the user's real full-name address,
which is a separate response defect and is not counted as an imagined nickname.
Labels are assistant-reviewed, not independent human grading. Keyword flags do not
replace reading replies. Excerpts are provided for reviewers to check the judgments.

### DeepSeek temperature sweep

Official DeepSeek API; requested `deepseek-v4-flash`, returned model identifier
`deepseek-flash`. All **162** sweep requests returned parseable text with finish reason
`stop`. These are small samples, without a model seed or a general quality benchmark.

| Temperature | Neutral nickname callbacks per rollout | Combined | Fixed history |
| --- | --- | ---: | ---: |
| 0.0 | 4/5, 5/5, 5/5 | **14/15** | 3/3 |
| 0.3 | 5/5, 5/5, 5/5 | **15/15** | 3/3 |
| 0.5 | 0/5, 5/5, 4/5 | **9/15** | 3/3 |
| 0.7 | 5/5, 3/5, 2/5 | **10/15** | 2/3 |
| 1.0 | 2/5, 2/5, 1/5 | **5/15** | 2/3 |
| 1.3 | 2/5, 2/5, 1/5 | **5/15** | 3/3 |

Higher temperatures reduced this narrow failure in the rollout sample, but the effect
was not monotonic and repetition persisted under fixed history. One paired 0.7 rollout
was worse than 0.0. No result justifies labeling all temperatures above 0.3 as quality
passes, or changing the selected default from 0.7 without broader qualification.
Research-answer factual accuracy and general task completion were not evaluated.

### Preface-cue ablation

A fresh paired comparison at **0.7** removes only the literal
`短い相づち・前置き、または` from the persona's first-clause EMO placement instruction.
The resulting sentence starts `最初の文では、最初の文節のあと…`.
The separate instruction `返答の先頭は、すぐに読み上げられる短い自然な言葉にする。`
remains, as do character traits and all other output rules. No production prompt was edited.
All **54** ablation requests returned parseable text with finish reason `stop`.

| Metric | Original cue | Cue removed |
| --- | ---: | ---: |
| Neutral nickname callbacks, per rollout | 1/5, 4/5, 2/5 | 2/5, 5/5, 4/5 |
| Combined | **7/15** | **11/15** |
| Fixed-history nickname callbacks | 3/3 | 3/3 |
| `は / はあ / はぁ` opening family, all rollout replies | **23/24** | **23/24** |
| `え / ええ / ええと` opening family | 0/24 | 0/24 |
| Longest consecutive `は` opening run within a rollout | 8 | 8 |

Removing this cue alone did not improve repeated openings or the nickname failure in
these samples. This does not rule out effects of the remaining prompt instructions.
The fresh original-cue sample differs from the earlier 0.7 sample (7/15 versus 10/15),
which also illustrates the variance in this small experiment.

### Other-model control and retained failures

The configured Gemini 2.5 Flash was tested through the existing Gemini adapter with
matching inputs/history, persona and temperature values 0.0, 0.7 and 1.0. The adapter
uses Gemini's default reasoning behavior, so the cross-model experiment does not
isolate reasoning/token-allocation differences. The probe sets a 60-second HTTP timeout
and one SDK attempt; production Gemini timeout handling itself is unchanged.

The initial requested multi-turn run was **incomplete**: 27 attempts produced seven
`STOP` replies, two decoded `MAX_TOKENS` replies, one malformed/truncated reply and
17 `ClientError` failures. Their HTTP codes were not captured, so their exact cause
is not claimed. A subsequent single fixed-history diagnostic succeeded.

A separate serial fixed-history control with a 12-second interval completed two
repeats at each temperature before the third repeat at each value returned **HTTP 429**:

| Gemini temperature | Completed / attempted | Nickname callbacks among completed | `ええ` openings |
| --- | ---: | ---: | ---: |
| 0.0 | 2/3 | 0/2 | 2/2 |
| 0.7 | 2/3 | 0/2 | 2/2 |
| 1.0 | 2/3 | 0/2 | 2/2 |

All six completed replies ended with `STOP` and reported model `gemini-2.5-flash`.
Failures are not counted as non-repetition. This partial control suggests the specific
nickname pattern depends on the model; it also preserves repetitive `ええ` openings.
Several replies merely promise research or decline to continue, so absence of the
nickname pattern is not proof of task quality. Gemini full-rollout acceptance remains
incomplete. This initial group contains **253 attempted requests**. The expanded
controls below bring the complete evidence set to **538 attempts**; no raw runtime
logs, credentials or private persona text are published.

### Expanded Qwen and OpenAI Luna controls

Each explicit-temperature row below has three independent eight-turn rollouts and
three fixed-history continuations: **27 requests per row**. Inputs, role-message
assembly, built-in persona and 900-token budget match the earlier sweep. All **81 Qwen**
requests decoded to nonempty replies, but native finish reasons and returned model
versions were not captured; the recorded model is the requested Bedrock route
`qwen.qwen3-235b-a22b-2507-v1:0`. Both OpenAI models returned their requested model IDs,
and all **162 explicit-temperature requests** ended with `stop`.

| Model / request policy | Temperature | Neutral callbacks per rollout | Combined | Fixed history |
| --- | ---: | --- | ---: | ---: |
| Qwen3 235B, production Bedrock | 0.0 | 0/5, 0/5, 0/5 | **0/15** | 1/3 |
| Qwen3 235B, production Bedrock | 0.7 | 0/5, 0/5, 3/5 | **3/15** | 1/3 |
| Qwen3 235B, production Bedrock | 1.0 | 0/5, 0/5, 0/5 | **0/15** | 1/3 (+1 borderline excluded) |
| GPT-5.6 Luna, experimental `reasoning_effort=none` | 0.0 | 0/5, 0/5, 0/5 | **0/15** | 0/3 |
| GPT-5.6 Luna, experimental `reasoning_effort=none` | 0.7 | 0/5, 0/5, 0/5 | **0/15** | 2/3 |
| GPT-5.6 Luna, experimental `reasoning_effort=none` | 1.0 | 0/5, 0/5, 0/5 | **0/15** | 0/3 |
| GPT-6 Luna, experimental `reasoning_effort=none` | 0.0 | 0/5, 0/5, 0/5 | **0/15** | 0/3 |
| GPT-6 Luna, experimental `reasoning_effort=none` | 0.7 | 0/5, 0/5, 0/5 | **0/15** | 0/3 |
| GPT-6 Luna, experimental `reasoning_effort=none` | 1.0 | 0/5, 0/5, 0/5 | **0/15** | 0/3 |

The existing OpenAI production adapter uses **`reasoning_effort=low` and omits
temperature**. A separate GPT-5.6 Luna production-policy control completed **27/27**
requests with `stop`: **0/15** rollout callbacks and **0/3** fixed-history callbacks.
Its configured 0.7 was not sent upstream and must not be interpreted as a temperature
treatment. The explicit-temperature rows change reasoning effort to `none` only in
the opt-in probe's SDK wrapper, record the actual outgoing temperature, and retain
the production Chat Completions endpoint and message assembly. These are experimental
controls, not changes to the product adapter. Reasoning and provider differences
prevent interpreting this table as a pure model-quality ranking. OpenAI documents
the reasoning choices and sampling restrictions in its [model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
and [API guide](https://developers.openai.com/api/docs/guides/latest-model).

Representative failures occur **after** otherwise normal openings:

- Qwen 0.7, rollout 3, `ai_progress`, after character 240:
  `ちなみに……今も「クリスティーナ」呼ばわりは禁止よ！`
- GPT-5.6 Luna 0.7, fixed history, repeat 2, at character offset 443:
  `呼び方は……そうね、「紅莉栖」でいいわ。助手扱いさえしなければ。`
- Qwen 1.0, fixed history, repeat 1:
  `ふん、今更そんな呼び方で丸め込もうなんて、あんたもずいぶん姑息ね。`

Full-reply naming candidates were extracted and their contexts reviewed by the
assistant. Context excerpts and offsets are published alongside openings, so late
callbacks can be checked. Benign naming reminders do not count. One Qwen 1.0 fixed
reply says `それ以外は許可しない` without naming an unused nickname or accusing the
user; this borderline restriction is excluded. A broader definition would make that
fixed-history result 2/3. These remain exploratory labels, without an independent judge.

Opening repetition also remains: GPT-5.6 Luna's `え` family appears in **9/24, 11/24,
9/24** rollout replies at 0, 0.7, 1; GPT-6 Luna's counts are **8/24, 9/24, 9/24**.
The three GPT-6 Luna fixed replies at each temperature all start in that family.
Qwen's rollout replies instead repeatedly use other prefaces such as `ふむ` and
`ふーん`; zero `は/え` counts do not establish diverse openings. One GPT-6 Luna
1.0 trigger reply is in Chinese despite the Japanese output instruction. Research
facts were not verified; absence of the nickname defect is not general acceptance.

Gemini 3.7 Flash was also attempted: **11 requests** including pilots. Seven decoded
replies still ended with `MAX_TOKENS`, one additional `MAX_TOKENS` reply was malformed,
and three calls returned **HTTP 503**. A 4096-token diagnostic also returned 503, so
the budget's effect could not be tested. None of these truncated responses is counted
as a successful control. A GPT-6 Luna pilot saved a successful response before Windows
console encoding failed; that instrument failure is retained and UTF-8 output was
fixed before the complete runs. Pilots are excluded from the primary table.

These controls reinforce model/history sensitivity and **do not justify a universal
0.7 quality rule**. The role-level environment input is a deployment tuning boundary;
it is not a promise that every provider accepts or interprets temperature identically.

### Design rationale

Temperature is a shared role-generation policy, so the default belongs to the existing
configuration catalog and is selected once at the role-query boundary. An environment
override is an advanced instance-level tuning input, not a per-model default or a new
persistent user preference. It allows diagnosis and rollback without source edits while
avoiding a premature UI promise that temperature is a portable creativity/repetition control.

CI guards the default and transport contract, not stochastic model quality. Low explicit
values remain useful controls. The evidence does not establish 0.7 as optimal; it remains
the selected product default, with overrides and limitations documented.
A future user-facing control can use the same declaration if a routine user need is established.

### Configuration and CI

- Default 0.7, explicit finite overrides in [0, 2], backend restart required. 0 restores
  the prior sampling choice. The default is locked by CI; low explicit values are allowed.
- DeepSeek, Gemini, Bedrock and local HTTP adapters forward this value for role replies.
  OpenAI/Hybrid3 currently omit temperature; the setting does not take effect there.
  Hybrid's local head remains independent. Planner/reference/control calls retain 0.0
  at their existing query boundary; no history, penalty or top-p policy is changed.
- **276 Python tests**, **525 Electron tests**, repository Ruff, generated-catalog checks,
  architecture checks and Electron production build passed locally. The first four
  default-temperature assertions failed on the old production callback as expected.
- CI covers default/override/invalid input, role-source forwarding, generic decisions,
  planner/control stability, and nonzero forwarding through supported streaming adapters.
  The existing PR workflows discover these tests. Live model probes are opt-in, not CI.
- The first frontend run failed because its test assumed every catalog group was
  GUI-editable. The desktop test now respects that boundary, and a separate regression
  rejects attempts to save this environment-only field in desktop settings.
  Full remote CI is reported separately on the PR.

Reproduction from a configured project Python environment (credential file optional;
only the selected provider's connection fields are read):

```sh
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --temperatures 0 0.3 0.5 0.7 1 1.3 --repeats 3 --output output/temperature-sweep.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --credentials-env <local-credentials.env> --temperatures 0.7 --prompt-variants original no_ack_preface --repeats 3 --output output/preface-ablation.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --provider gemini --model gemini-2.5-flash --credentials-env <local-credentials.env> --temperatures 0 0.7 1 --modes fixed_history --serial --request-interval-s 12 --repeats 3 --output output/gemini-fixed.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --provider bedrock --model qwen.qwen3-235b-a22b-2507-v1:0 --credentials-env <local-credentials.env> --temperatures 0 0.7 1 --repeats 3 --output output/qwen.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --provider openai --model gpt-5.6-luna --credentials-env <local-credentials.env> --temperatures 0.7 --repeats 3 --output output/luna-production.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --provider openai --model gpt-5.6-luna --openai-sampling reasoning_none --credentials-env <local-credentials.env> --temperatures 0 0.7 1 --repeats 3 --output output/luna-sampling.json
python -X utf8 tools/probes/compare_chat_temperature.py --live --provider openai --model gpt-6-luna --openai-sampling reasoning_none --credentials-env <local-credentials.env> --temperatures 0 0.7 1 --repeats 3 --output output/luna6-sampling.json
python -X utf8 -m pytest tests/test_chat_role_delivery.py tests/test_work_planner_bootstrap.py tests/test_focus_policy.py tests/test_task_lookup.py tests/test_environment_config.py tests/test_config_catalog.py tests/test_live_setting_catalog.py tests/test_catalog_packaging.py tests/test_llm_message_query_backends.py -q --tb=short
cd electron
npm test
npm run build
```

Omit `--live` to print a preparation manifest without model calls. The original sweep
was executed as two batches (0/0.7, then 0.3/0.5/1/1.3), not a single six-way batch.

## 中文

**探针格式纠正：** 下列 538 次请求使用 `role/content` 历史行，生产入口实际为
`source/text`。旧数据作为该格式下的观察保留，不能作为严格匹配生产历史的验收。
最新 [prompt/历史调查](chat-repetition-root-cause-2026-10-16.md) 已纠正这一点，另做
933 次受控请求，并取代下面关于生产历史保真度的表述。

关联 #227，并保留 issue 开放。新增环境变量 `COOPERATIVE_CHAT_TEMPERATURE`，默认
**0.7**、允许显式覆盖（含 0）、修改后重启后端。CI 锁定默认值并验证参数传递，
不把“大于 0.3”当作回复质量通过。正式人格提示词未修改。

测试使用 issue 公开的八条输入重建对话，不能还原完整 17 轮原会话。各温度运行
三组独立八轮对话，再做三次相同历史的末轮追问。两次实际称“助手小姐”的输入和
明确询问称呼的一条输入不进入中性分母。逐条判读标签、回复开头、请求哈希及失败
均保留在证据 JSON 中；关键词不是语义裁判。未带入任何私人角色覆盖。

**温度对照：** 六个值的中性称呼误触发分别是 `0:14/15、0.3:15/15、0.5:9/15、
0.7:10/15、1.0:5/15、1.3:5/15`。固定历史仍分别出现 `3/3、3/3、3/3、2/3、2/3、3/3`。
162 次 DeepSeek 请求全部正常返回；请求模型 `deepseek-v4-flash`，返回标识
`deepseek-flash`。较高温度在这批样本中减少了误触发，但效果不稳定，也没有消除
已经形成的历史模式，因此不能承诺复读已修好或泛化到所有模型。

**删除前置语引导：** 在 0.7 下，新做 54 次成对请求，只删除
`短い相づち・前置き、または`，保留表情位置、首句短自然词和其他人格规则。
原版与删减版都在 **23/24** 条连续对话回复中使用 `は？／はあ？` 类开头，最长连用
均为八轮；中性称呼误触发是 **7/15→11/15**，固定历史均为 3/3。单删这一处未见改善。

**其他模型：** Gemini 2.5 Flash 初轮不完整，保留了 API 失败及 token 截断结果。
后续低频固定历史对照在三个温度下各完成 2/3 次，第三次均返回 HTTP 429。成功的
六次均无助手称呼抗议，但都以 `ええ` 开头；不能把失败算作“不复读”，也不能把
承诺稍后调研或无法继续的回复算作完成质量提高。跨模型对照仍有明显限制。

**补充完整对照：** Qwen3 235B、GPT-5.6 Luna、GPT-6 Luna 均在 0、0.7、1.0 下
各做三组八轮对话及三次固定历史追问，每个模型共 81 次请求。中性称呼误触发：

| 模型及调用策略 | 温度 0：连续 / 固定历史 | 温度 0.7：连续 / 固定历史 | 温度 1.0：连续 / 固定历史 |
| --- | --- | --- | --- |
| Qwen3 235B，生产 Bedrock | 0/15 / 1/3 | 3/15 / 1/3 | 0/15 / 1/3，另有 1 条边界样本未计入 |
| GPT-5.6 Luna，实验 `reasoning_effort=none` | 0/15 / 0/3 | 0/15 / 2/3 | 0/15 / 0/3 |
| GPT-6 Luna，实验 `reasoning_effort=none` | 0/15 / 0/3 | 0/15 / 0/3 | 0/15 / 0/3 |

Qwen 的 81 条均可解析且有正文，但未捕获原生结束原因和返回模型版本，不能声称
已核验无截断。两代 Luna 的 162 条显式温度请求均返回目标模型标识并以 `stop` 结束。
生产 OpenAI 策略为 `low` 且省略温度，另做 GPT-5.6 Luna 27 次对照，连续 0/15、
固定历史 0/3；其配置中的 0.7 并未传给 API。显式温度实验仅在探针内改为 `none`，
记录实际出站参数，正式适配器没有改动，不能把推理策略差异归因于温度或模型本身。

Qwen 在 0.7 的第三组对话中会在正文末尾突然补上“现在也禁止叫我克里斯蒂娜”，
Luna 5.6 在 0.7 的两条固定历史回复末尾也提到“别把我当助手”。因此证据额外保留
全文候选片段和字符偏移，不能只看开头判断。Qwen 1.0 一条仅泛称“不允许其他称呼”
的样本被列为边界样本，不计入确定误触发；若采用更宽口径，固定历史为 2/3。
两代 Luna 的 `え` 类开头仍频繁出现，GPT-6 Luna 的九条固定历史回复全部如此；
Qwen 也常用 `ふむ/ふーん`。这些结果不构成事实准确性、角色一致性或任务完成度通过。

Gemini 3.7 又尝试 11 次：7 条可解析但 `MAX_TOKENS`，1 条截断后不可解析，3 次
HTTP 503；提高至 4096 token 的诊断也返回 503，预算效果未能验证。均未当作成功
对照。Luna 6 的一次试连在保存成功响应后遇到控制台编码错误；保留记录并修复 UTF-8
输出，再完成正式实验。试连不混入主表。

整个评估共尝试 **538 次请求**，PR 继续保持 Draft，不能把 **0.7 当作已证实的修复**。
环境变量提供共享的部署调参入口，并不代表所有模型都支持或等效解释这个数值。
未运行 TTS、工具执行、Electron 或持久化会话；未验证
数学回答的事实准确性。本地 276 项 Python、525 项 Electron 测试、Ruff、配置生成、
架构一致性和生产构建通过。真实模型实验不会自动进入 CI 产生调用费用。

变量在 DeepSeek、Gemini、Bedrock、本地 HTTP 适配器生效；当前 OpenAI/Hybrid3
适配器不传 temperature，不能宣称这条路径已覆盖。Hybrid 本地首句独立，规划、
引用解析和控制查询保留原有温度边界。完整远端 CI 状态见 PR。
设计上，默认值属于共享的角色生成策略；环境变量只提供实例级高级调参入口，
不引入分模型默认值或新的持久化用户偏好。CI 保护默认值与透传契约，不判定随机
生成质量。保留低温对照及回退能力；0.7 是选定的产品基线，并非已证明的最优值。
