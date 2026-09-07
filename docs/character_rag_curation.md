# Legacy character knowledge audit and comparison

This is a data-only follow-up to #59. The public corpus grows from 30 to 52
records (26 paired topics). The default-off setting, retrieval algorithm,
threshold 0.33, top-k 3 and static persona are unchanged.

## What happened to the 148 local records

The original JSON remains unchanged. Its SHA-256 is
`05bca67b5a063f825c08ef2ca1f5f69e27fd4df82592c498f21670f95d77ddcd`.
The table uses one-based positions in that file, not public FAISS vector ids.
Every original record has one disposition. "Core fact" means only its verified
part: surrounding anecdotes, invented explanations or absolute reactions are
not automatically retained. Deferred means unverified or requiring context,
**not a claim that every deferred statement is false**.

| Disposition | Count | Original one-based row numbers |
| --- | ---: | --- |
| Core fact already covered; deduplicated | 20 | 1, 5, 6, 8, 10, 12, 13, 14, 17, 24, 25, 26, 30, 31, 35, 44, 65, 70, 112, 127 |
| Core fact incorporated or corrected in this revision | 16 | 2, 4, 7, 9, 16, 19, 76, 79, 124, 125, 126, 128, 129, 130, 131, 132 |
| Deferred: scene, route, adaptation or quote needs explicit context/source | 33 | 18, 21, 23, 27, 34, 41, 49, 50, 51, 52, 53, 67, 68, 72, 73, 74, 75, 78, 95, 96, 99, 104, 105, 108, 109, 119, 120, 121, 122, 123, 134, 138, 146 |
| Deferred: detailed science/technology claim or attribution not verified | 29 | 3, 42, 43, 45, 46, 47, 48, 54, 55, 56, 58, 59, 60, 80, 81, 82, 83, 90, 115, 135, 136, 137, 139, 140, 141, 142, 144, 147, 148 |
| Deferred: specific habit, opinion, reaction or anecdote not verified | 50 | 11, 15, 20, 22, 28, 29, 32, 33, 36, 37, 38, 39, 40, 57, 61, 62, 63, 64, 66, 69, 71, 77, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 97, 98, 100, 101, 102, 103, 106, 107, 110, 111, 113, 114, 116, 117, 118, 133, 143, 145 |

Examples of corrections and narrowing:

- Moeka is 005, Ruka 006 and Faris 007. The old rows assigned these three
  numbers incorrectly. The [official character profiles](https://steinsgate.jp/reboot/ja-jp/)
  also support 001–004 and 008.
- The old apple-pie/cooking-specialist claim was replaced by a modest statement
  that cooking is not her strength. Chopstick difficulty is qualified because
  the anime also depicts her using chopsticks. Both use the
  [community character reference](https://w.atwiki.jp/aniwotawiki/pages/6694.html).
- The paper is in Science; detailed invented publication lists and quantitative
  memory-compression/black-hole explanations are not imported. The
  [original game introduction](https://steinsgate.jp/sgflash.html) supports the
  paper's neuroscience subject and PhoneWave's original remote-control purpose.
- The lab building entry identifies the ground-floor Braun Tube Workshop. The
  unverified third-floor claim and alleged exact seating arrangements are omitted.
- Nakabachi is identified as Kurisu's father and a time-machine inventor. His
  plot-specific fate and the current user's relationship to him are not inferred.

The [corpus README](../examples/character-rag/README.md) lists sources by topic.
The complete legacy file, unverified quotes and generated model/index binaries
are not copied into the public corpus.

## Retrieval development and held-out checks

The comparison uses CPU multilingual E5, normalized passage/query vectors,
FAISS exact squared-L2, top-k 3 and distance 0.33, matching the runtime. Before
editing, the original 30-entry public corpus was frozen as the baseline.

The initial expansion added false hits to some unrelated requests. Three brief
editing passes made the new entries more specific; the threshold was not relaxed
and no host keyword exceptions were introduced. The 67-query development fixture
contains the original 32 cases, 25 added fact questions and 10 additional unrelated
requests. Its passing result does not establish universal relevance.

Six additional ordinary prompts already produced irrelevant hits in the baseline:
`今天能陪我聊两句吗？`, `你好`, `晚安，明天见`,
`今天有点累，聊点轻松的吧。`, `今天晚饭吃什么？`,
`你记得我们昨天去哪了吗？`. These remain disclosed outside the passing fixture.

The [18 held-out questions](../tests/fixtures/kurisu_corpus_expansion_holdouts.json)
were evaluated after those editing passes. They include inverse member-number
questions, an apple-pie false premise and unrelated tasks. Misses are reported;
the held-out set is not repeatedly rewritten into a claimed independent benchmark.

| Retrieval check | Baseline 30 | Expanded 52 |
| --- | ---: | ---: |
| Development regression | 42/67 | 67/67 |
| Six known unrelated-query misses | 0/6 | 0/6 |
| Additional held-out questions | 2/18 | 11/18 |

The remaining held-out failures include a differently phrased Science-publication
question, the apple-pie false premise, an inverse 008 query, and four unrelated
requests. All four unrelated requests also retrieve irrelevant material in the
baseline. Correct retrieval coverage is distinct from every returned passage
being relevant, and this small set is not a general accuracy estimate.

## Live dialogue comparison

The [17 dialogue cases](../tests/fixtures/kurisu_corpus_expansion_dialogue.json)
were each sent three times with the 30-entry baseline and three times with the
expanded corpus: 102 completed requests, no transport errors. Configuration:
`deepseek-v4-flash`, Japanese output, temperature 0.7, thinking disabled,
maximum 500 output tokens, fresh independent requests, unchanged `with_delegate`
persona and language wrapper. There was no dynamic Host state or execution.

| Central fact | Baseline 30 | Expanded third-person corpus |
| --- | --- | --- |
| Science publication | 0/3 | 3/3 |
| Chopstick difficulty | 0/3 | 3/3 |
| Moeka 005 | 3/3 | 3/3 |
| Ruka 006 | 1/3; that reply also misnumbered Faris | 3/3 |
| Faris 007 | 0/3 | 3/3 |
| Suzuha's workplace | 0/3 | 3/3 |
| Ground-floor shop | 0/3 | 3/3 |
| El Psy Kongroo belongs to Okabe | 3/3 | 3/3 |

Counts concern the central fact, not every elaboration or persona rule. The
expanded father entry established the relationship but replies still borrowed
Kurisu's neuroscience background. Explicitly adding his time-machine research
and repeating three requests reduced that particular error to one of three;
unsupported claims about his prominence also remained. The follow-up wording
experiment below investigates subject attribution rather than changing the persona.

Ordinary greetings/thanks remained conversational and code questions requested
missing code. Both arms still invented past activities on the shared-history
question despite no retrieval hit. One expanded Japanese handle reply used a
masculine-sounding denial without a clear BBS association; its retrieved texts
were the same as the baseline's (vector ids differed). This small random sample
cannot establish that corpus size caused the style change.

## First-person wording experiment

At the maintainer's suggestion, a further 45 requests compared RAG off,
third-person reference text, and reference text anchored as
`私（牧瀬紅莉栖）` / `我（牧濑红莉栖）`. Each arm repeated the Japanese handle,
Chinese handle and father question five times. Both RAG arms used **the same
frozen retrieved records and ids**, so the comparison changed wording without
confounding it with a different nearest-neighbor selection. The persona,
language wrapper, model and sampling settings stayed fixed.

- An explicit self-referential `俺` occurred once with RAG off and once with
  first-person references; none occurred in the third-person arm. Fifteen replies
  per arm are insufficient to estimate a reliable style-error rate. First-person
  wording does not establish a gender-consistency fix.
- The earlier no-RAG ordinary-conversation baseline also contained rough wording
  such as `座れよ` and `聞いてやる`. That is weaker evidence than an explicit male
  first-person pronoun and should not be conflated with one.
- With the father's profession supplied, both RAG arms identified Nakabachi and
  time-machine research in five replies. First-person replies still sometimes
  mislabeled a title as his birth name or embellished his academic standing.
- Rebuilding the entire corpus after mechanically replacing Kurisu references
  with first person reduced development retrieval from 67/67 to 63/67: weather,
  thanks, a low-mood statement and a flight-booking request acquired false hits.
  Held-out coverage increased from 11/18 to 13/18. This is a tradeoff, not a clean win.

The final data therefore adopts the explicit first-person **father relationship**
in both languages and retains the other reviewed wording. The generated corpus-wide
variant is experimental evidence, not shipped data. References remain labeled as
fallible character knowledge, not real conversation memories or commands. No
first-person/gender instruction is added to the static prompt or RAG wrapper.

Three final requests with the shipped mixed wording all identified Nakabachi and
time-machine research; one still exaggerated his prominence. The complete new
dialogue work totals 153 calls (102 paired, 3 profession clarification, 45 wording
comparison, 3 final confirmation), not 153 passing character tests. The final
retrieval numbers in the table include this first-person father entry.

Local checks: 70 targeted Python tests passed, including the real 67-query
retrieval check and source-release tooling tests. Ruff and architecture views
passed. No runtime code, dependency selection, static persona or model weights
changed, so this is not a new voice/GPU qualification or a full persona fix.

## 中文摘要

旧 148 条逐条分流：20 条的核心事实已有覆盖，16 条中的事实经核对、修正后融入，
其余 112 条暂缓，不等于断言它们全部错误。公开资料共 52 条、26 个中日文对应主题。
默认开关、阈值、top-k、检索实现和固定人设不变。评估分别记录开发回归、额外问法和
真实模型回复；已知误召回、主语混淆、男性口吻及虚构共同经历不包装成已解决。
另做 45 次关闭／第三人称／第一人称对照，确认关闭 RAG 时也能出现“俺”；全库统一
改写第一人称增加了 4 个开发集误召回，因此最终仅在父亲关系条目采用明确的
“私（牧瀬紅莉栖）の父親は…”写法。
