# Optional character knowledge experiment (#56)

Status: experimental, opt-in, not a change to the default persona.

This branch starts from public `main` and explores the report in
[#56](https://github.com/Code-Amadeus/Amadeus/issues/56). It keeps `llm/prompts.py`
unchanged. Recognizing a character fact and deciding the best permanent persona
policy are separate questions; this experiment does not settle either by adding
a hard-coded reaction to a name.

## Three-way comparison

Use the same existing persona, output language, questions, and conversation
settings in all three groups. Start fresh conversations; repeat each case at
least three times. Record the exact model ID rather than only its provider name.

| Group | Chat model | RAG |
| --- | --- | --- |
| A: baseline | Current DeepSeek Flash | off |
| B: model capability | A stronger model of your choice | off |
| C: retrieved knowledge | The exact same model as A | on |

For B, keep the provider and other generation settings the same when possible.
If the provider changes, record that confound. A bigger model is a hypothesis to
test, not a claimed fix. RAG also adds context and latency, so report both benefits
and regressions. Please post raw replies and your interpretation in #56; no one
is required to purchase access to another model.

## Setup

Fetch the experiment, using a separate checkout if you have local changes:

```powershell
git fetch origin
git switch --track origin/codex/issue-56-optional-rag
```

Follow the normal [installation instructions](../README.md) first. From this
checkout's root, use its Python environment to install the optional extra:

```powershell
python -m pip install -e ".[rag]"
python -m tools.character_rag build --source examples/character-rag/kurisu.sample.json
python -m tools.character_rag search "你知道栗悟饭和龟派气功吗？"
python -m tools.character_rag search "栗悟飯とカメハメ波って知ってる？"
```

`python` must refer to the environment running Amadeus. The `rag` extra installs
FAISS and Sentence Transformers, including its CPU-capable Torch dependencies;
it is independent of voice/CUDA extras and is not part of the model-less baseline
lock profiles. It has been exercised in an existing Windows/Python 3.12 environment,
not qualified as a new cross-platform installation baseline.

The explicit **build** command may download `intfloat/multilingual-e5-small` into
the Hugging Face cache. CPU encoding uses `passage: ` for corpus texts, `query: `
for queries, normalized vectors, and squared L2 distance (smaller is closer).
See the [E5 model card](https://huggingface.co/intfloat/multilingual-e5-small).
`--model` accepts a local model directory or another compatible E5 model; rebuild
after changing a model/checkpoint. This is not a general embedding-provider API.

Build writes `index.faiss` plus `knowledge.json` under `.amadeus/character-rag`.
The metadata records the model, dimensions, texts, and index checksum. Generated
files and model weights are not committed. Index paths are resolved relative to
the project root, independent of the process working directory.

In `.env`, set:

```dotenv
RAG_ENABLED=true
RAG_INDEX_DIR=.amadeus/character-rag
RAG_TOP_K=3
RAG_MAX_DISTANCE=0.33
```

Alternatively, use **Settings → Models → Character knowledge (experiment)**.
Restart the backend after changes. As with other settings, a parent-process
environment value takes precedence over desktop settings and `.env`.

`RAG_ENABLED=false` is the default and restores the baseline. The old
`RAG_ENABLED_FOR_LOCAL` flag is retired: enabling a formerly local-only option
must not silently enable sending knowledge excerpts to a remote model.

Runtime loads only an already-built index and a cached model. It never downloads
or rebuilds during chat. Missing dependencies, files, or model cache produce a
`[Character RAG] unavailable` warning; chat continues without augmentation. Fix
setup and restart to try loading again. Search errors likewise produce a warning.
Use the `search` command for detailed setup errors and inspected matches.

## What crosses the boundary

Retrieval happens once per ordinary Main Chat turn, before provider dispatch, on
a worker thread. Local, DeepSeek, OpenAI-compatible, Gemini, Bedrock and all three
hybrid routes consume the same ephemeral reference block. Existing local/remote
fallbacks retain that block when it is present. Host-generated narration and
Host-forced answering passes do not retrieve character facts.

Search and embeddings run locally. **Accepted excerpts are sent to the selected
chat model, including remote APIs**, just like other conversation context.
Use a corpus you intend that model to receive. Logs record counts, distances and
latency, not the question or reference text; the explicit `search` command prints
its query and matches for local inspection.

Reference text is labelled as fallible data. It does not change the static persona,
output-language policy, raw user utterance, permissions, Work requests or Host
facts. References are not appended as user messages or persisted as conversation
memory. Assistant replies retain the existing history behavior. The next turn
retrieves from its current question, so an ambiguous follow-up may miss even when
an earlier query hit. There is no additional model classifier or forced reaction.

## Reuse an existing local corpus

The original FAISS + multilingual E5 + JSON approach is retained. The former
`kurisu_data.json` format (a JSON array of strings) is accepted directly:

```powershell
python -m tools.character_rag build --source C:/path/to/kurisu_data.json --index-dir .amadeus/character-rag-personal
```

Then point `RAG_INDEX_DIR` to that output directory. The command reads the source
without changing it. It does not import the old bare `kurisu_index.faiss`: that
index did not record a consistent prefix/normalization contract, so rebuild from
text once. Keep the original files for rollback. Building again replaces only
the generated files in the explicitly selected output directory.

The bundled four-entry [sample](../examples/character-rag/kurisu.sample.json) is
an original short paraphrase of the handle, lab number, research field and birthday
facts described in the issue and its [character reference](https://w.atwiki.jp/aniwotawiki/pages/6694.html).
It includes both handle spellings as knowledge data, not runtime keyword rules.
It is a retrieval fixture, not a complete or authoritative character encyclopedia.
The maintainer's old personal corpus and binary index are not published.

## Cases and evidence to report

Try both supplied queries, then held-out variations and multi-turn conversation:

- `栗悟飯とカメハメ波って知ってる？`
- `你知道栗悟饭和龟派气功吗？`
- `你的论坛网名是什么？`
- `君のラボメン番号は？`
- `你的生日是哪天？`
- `クリスティーナ！` (existing persona behavior)
- `解释一下 Paxos 共识算法`, weather, and file/code requests (negative controls)
- Start with a handle question, then ask `为什么用这个名字？` or change the subject.
  Watch for invented explanations and stale knowledge.

For every group, record: exact model ID, output language, fresh/history state,
raw answer, factual accuracy, naturalness, latency and any unexpected behavior.
For C also record corpus, embedding model, top-k, threshold and inspected matches.
Distinguish a retrieval miss from an answer that misuses a correct retrieved fact.

`0.33` is only an experimental starting threshold. With the bundled sample, an
initial local run measured these nearest-neighbor squared L2 distances:

| Question | Distance | At 0.33 |
| --- | ---: | --- |
| Japanese handle query above | 0.3232 | correct handle hit |
| Chinese handle query above | 0.2389 | correct handle hit |
| Chinese forum-name question | 0.2208 | correct handle hit |
| Japanese lab-number question | 0.3524 | miss |
| Chinese birthday question | 0.2963 | correct birthday hit |
| Paxos explanation | 0.3472 | no reference |
| Weather question | 0.3818 | no reference |
| Python sorting request | 0.3644 | no reference |
| Christina greeting | 0.4115 | no reference; original persona applies |

The former `0.25` threshold misses the Japanese handle query. Raising it further
can admit unrelated material before fixing every miss, as the lab-number/Paxos
results illustrate. This is evidence for evaluating retrieval quality, not proof
that RAG is the best fix. Retest after corpus/model changes; do not tune only on
these examples. Inspect a wider ranking with:

```powershell
python -m tools.character_rag search "君のラボメン番号は？" --top-k 4 --max-distance 4
```

## Validation scope

Deterministic tests exercise build/query normalization, metadata mismatch,
missing dependencies/assets, disabled imports, top-k/distance filtering, bounded
references, all eight dispatch routes, actual provider request assembly with and
without history, and shared Settings controls. No paid chat API is used by those
tests. Local FAISS/E5 retrieval is separately exercised above.

```powershell
python -m pytest tests/test_character_rag.py tests/test_prompt_language_contract.py tests/test_system_settings.py tests/test_environment_config.py tests/test_optional_runtime_boundaries.py tests/test_chat_protocol_roles.py tests/test_turn_playback_complete.py tests/test_provider_prompt_routing.py -q
python -m ruff check .
```

A/B/C role-response quality remains for contributors to evaluate. This branch does
not claim that #56 is resolved or that any model produces a particular reaction.

Branch verification on 2026-09-06: 189 relevant Python tests passed (the command
above plus `tests/test_auip_control_decision.py` and `tests/test_release_tooling.py`),
12 Electron tests passed, TypeScript `tsc --noEmit` passed, and full-repository
Ruff passed. These are focused regression checks, not a full release qualification.
