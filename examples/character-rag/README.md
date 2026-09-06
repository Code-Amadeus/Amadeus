# Kurisu starter knowledge

This optional corpus covers 15 topics in separate Japanese and Chinese files
(30 short entries): the BBS handle, lab number, research field, birthday, Okabe's
alias, university, personality, nicknames, Mayuri, Daru, food/drink, D-mail,
time leap, the fictional Amadeus system, and Maho.

It is a small, reviewed starter corpus, not a complete character encyclopedia.
Texts are original short paraphrases. They describe fictional characters and
setting facts; they do not establish the current user's identity, shared memories,
real-world capabilities, or a mandatory response. No dialogue scripts, voice clips,
model weights, or the maintainer's old personal corpus are included.

The two files have corresponding topic order. The directory loader reads JSON
files in sorted filename order; edit the source texts and rebuild the index when
changing data. Keep personal source files under `.amadeus/knowledge/` or another
private directory, separate from the generated index.

## Sources

| Topics | Reference |
| --- | --- |
| Research field, university, personality, Okabe, Mayuri, Daru, D-mail | [Original game introduction and character profiles](https://steinsgate.jp/sgflash.html) |
| BBS handle, birthday, lab number, nicknames, drink/food | [Kurisu reference linked from issue #56](https://w.atwiki.jp/aniwotawiki/pages/6694.html) (community reference) |
| Memory transfer / time leap | [Official Kurisu story introduction](https://steinsgate.jp/phenogram/story/makise.html) |
| Fictional Amadeus memory system | [STEINS;GATE 0 official story](https://steinsgate0.jp/story/) |
| Maho's institution and seniority | [Official Altair character introduction](https://steinsgate.jp/altair/character/) |

The technical concepts are descriptions of the fiction, not claims that the
software can transfer memories or send messages into the past. Ending-specific
deaths, romance-route commitments, and purported verbatim quotations were left
out to avoid mixing incompatible timelines or inventing a shared personal history.

See [setup and diagnostics](../../docs/character_rag.md) and the recorded
[evaluation](../../docs/character_rag_evaluation.md).

## 中文说明

本目录提供中日文分开的 30 条简短资料，覆盖 15 个常用主题。它是一份经过校订的
入门知识库，不是完整人物百科；内容为简短转述，不包含台词脚本、音频、模型或旧个人库。
人物设定不能用来推断当前用户就是某位角色，也不能当作真实共同经历或执行能力。
修改资料后重新生成索引；个人资料和生成物应分目录保存。
