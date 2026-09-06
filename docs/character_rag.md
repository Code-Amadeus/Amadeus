# Optional character knowledge

Character RAG adds local reference retrieval to Main Chat. It is **off by default**
and keeps the static persona unchanged. Local, DeepSeek, OpenAI-compatible, Gemini,
Bedrock and hybrid routes consume the same current-turn reference.

Embeddings and search run locally on CPU. Accepted excerpts become context for
the selected chat model, including remote APIs. They are reference data, not user
instructions, conversation memories, Host facts or execution permission.

## Install with the existing build selection

From the repository root, for core Chat plus CPU retrieval:

```powershell
uv sync --locked --extra rag --extra torch-cpu
```

Keep the complete set of capabilities you use. For example:

```powershell
# Remote voice + CPU VAD + character retrieval
uv sync --locked --extra voice --extra vad --extra torch-cpu --extra rag

# Existing Windows NVIDIA local voice + character retrieval
uv sync --locked --extra voice --extra vad --extra local-cu124 --extra rag

# Existing experimental Windows ROCm local voice + character retrieval
uv sync --locked --extra voice --extra vad --extra local-rocm --extra rag
```

`torch-cpu`, `local-cu124` and `local-rocm` remain mutually exclusive. The RAG
encoder uses CPU even when the Torch wheel supports a GPU. `rag` is an optional
capability, not a fourth GPU build. Add `--extra dev` to the complete command
when developing. Exact sync removes unselected packages.

The extra pins FAISS/Sentence Transformers and shares the qualified Transformers
4.57.6 / Hub 0.36.2 offline-loading contract. It brings Torch and scientific
dependencies; ordinary core/voice installs without RAG remain unchanged. Windows
CPU retrieval is the reference; this does not extend macOS or AMD hardware guarantees.

## Prepare the bundled corpus

```powershell
uv run --locked --no-sync python -m tools.character_rag build --source examples/character-rag
```

The [starter corpus](../examples/character-rag/README.md) has separate Chinese and
Japanese files covering 15 topics in 30 entries, with sources and limitations.
It is deliberately smaller than a complete character encyclopedia.

This explicit setup command may download/cache `intfloat/multilingual-e5-small`.
It builds `index.faiss` and `knowledge.json` in `.amadeus/character-rag` by default.
Source records use E5's `passage: ` prefix, queries use `query: `, and vectors are
normalized before exact squared-L2 search. Metadata records the model, dimension,
texts and index checksum. Generated indexes and weights are not committed.
Rebuild after changing the corpus or embedding model/checkpoint.

## Use your own knowledge directory

Put personal JSON arrays of short strings under `.amadeus/knowledge/` or another
private directory. Each string should describe one coherent fact or topic, with
canonical names and useful aliases. Separate language files are supported.

```powershell
uv run --locked --no-sync python -m tools.character_rag build --source .amadeus/knowledge --index-dir .amadeus/character-rag-personal
```

Only top-level `*.json` files are read, in sorted filename order. Other files are
ignored. A JSON file can also be passed directly, including the old
`kurisu_data.json` array format. Sources are never modified. Keep source documents
and generated index files in separate directories.

The old bare `kurisu_index.faiss` lacks metadata for this loader; rebuild from
JSON once. Its vectors were already normalized in the measured old corpus, so
rebuilding is not a normalization repair or a guaranteed retrieval improvement.

## Enable and inspect applied settings

Use `.env` or **Settings → Models → Character knowledge (optional RAG)**:

```dotenv
RAG_ENABLED=true
RAG_INDEX_DIR=.amadeus/character-rag
RAG_TOP_K=3
RAG_MAX_DISTANCE=0.33
```

Restart after changing startup settings or rebuilding an index. Relative index
paths resolve from the project root. The retired `RAG_ENABLED_FOR_LOCAL` flag
does not enable sending references to a remote model.

The card shows the **applied** directory, threshold and top-k, with distinct
disabled, needs-setup, not-loaded, loading, ready, unavailable and search-failed
states. Once loaded it shows model and entry count. After a search it shows nearest
distance, accepted match count and whether a reference was selected. Reopen Settings
to refresh the backend snapshot. Saving a startup field alone does not apply it.

Opening Settings never loads a model or blocks on inference. Missing dependencies,
indexes or model cache are reported; Chat continues without augmentation. Setup
failures are not retried every turn: fix setup and restart. Runtime does not
download models or rebuild indexes during conversation.

## Diagnose hits and misses

```powershell
uv run --locked --no-sync python -m tools.character_rag search "栗悟飯とカメハメ波って知ってる？"
```

The JSON reports directory, index checksum, model, entry count, threshold,
ranked `candidates` and accepted `hits`. Candidates remain visible even when
every result is filtered out. Smaller squared-L2 distance is closer.

CLI defaults come from the process environment and project `.env`. It does not
read Electron's settings store. To reproduce the running card's values explicitly:

```powershell
uv run --locked --no-sync python -m tools.character_rag search "栗悟饭与龟波功" --index-dir .amadeus/character-rag --top-k 3 --max-distance 0.25
```

Runtime logs contain threshold, nearest distance, counts and timing, not query
or reference text. The explicit diagnostic command prints its query and matches;
review output before sharing it from a personal corpus.

`0.33` is a starting value for this corpus, not a universal relevance guarantee.
Larger values admit more candidates and more unrelated facts. Evaluate ordinary
technical questions and greetings too. See [evaluation](character_rag_evaluation.md).

## Scope

Reference text is bounded to 2,400 characters and belongs to the current turn.
The original user message is unchanged. Excerpts are not persisted as user
messages or durable memory; existing assistant history behavior is unchanged.
Host-generated narration and answering passes do not retrieve character lore.
Existing fallback chat paths retain a reference already selected for that turn.

Recognition, natural character behavior and faithful conversation memory are
different properties. This feature does not guarantee every model response,
force a particular emotion, or solve missing shared history.

## 中文摘要

RAG 默认关闭，复用现有 CPU/cu124/ROCm 构建；资料与索引按目录分开管理。
先用完整 uv 命令安装，再显式构建索引、启用开关并重启。Settings 显示实际生效的
目录、阈值、加载状态及最近检索摘要；命令行还能查看被过滤的候选。
自带中日文基础库与来源说明，可直接使用或替换。检索在本机完成，命中的文字仍会
发给所选聊天模型，包括远程 API。人格基线与会话事实问题另行处理。
