# Character RAG experiment history (#56)

Use [Optional character knowledge](character_rag.md) for current setup. The
original branch `codex/issue-56-optional-rag` at `0582665` used four entries;
its distances are historical evidence, not expected scores for the expanded corpus.

On 2026-09-06 a controlled comparison used the same Japanese persona, independent
requests, temperature 0.7, max_tokens 500 and thinking disabled. Three questions
were repeated three times across five groups (45 calls):

| Group | Chinese question | Chinese near-match | Japanese question |
| --- | ---: | ---: | ---: |
| Flash, no retrieval | 0/3 | 0/3 | 0/3 |
| Pro, no retrieval | 0/3 | 0/3 | 0/3 |
| Flash + original sample, 0.33 | 3/3 | 3/3 | 3/3 |
| Flash + rebuilt private 148-entry corpus, 0.33 | 3/3 | 3/3 | 0/3 |
| Flash + original sample, 0.25 | 3/3 | 0/3 | 0/3 |

Counts mean recognizable association with Kurisu's BBS identity, including clear
embarrassed denial. They do not score every style rule or incidental claim, and
this small non-thinking-mode test is not a general model ranking.

The original sample's Japanese query scored 0.3232 and its Chinese near-match
0.2530. Both miss a 0.25 cutoff. The contributor's actual configuration remains
unconfirmed, so this is a reproduced pattern rather than a confirmed diagnosis.

The private old index was intact and already normalized. Adding only the Japanese
canonical spelling to its handle entry reduced the rebuilt Japanese distance
from 0.3925 to 0.3172. That corpus also retrieved unrelated weather/Paxos material,
so raising the threshold was not a general solution.

See [current evaluation](character_rag_evaluation.md). Optional RAG does not claim
to fix the default-off Main Chat acceptance criterion in #56.
