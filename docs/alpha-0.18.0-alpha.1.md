# Amadeus 0.18 Alpha 1

This source preview bundles `eidema==0.2.0a3` from the independent
[Eidema SDK](https://github.com/Code-Amadeus/Eidema).

## Memory settings

Open **Settings → General → Memory**. The panel reports the version of Eidema
installed in the backend. Both switches are off by default. They are saved
independently and apply after restarting the backend.

- **Let the character look up memories** allows the character to look up
  saved notes and permitted conversation evidence. A cloud character's
  model provider receives the evidence for the reply and may also receive
  candidate excerpts for selection, including excerpts left out of the final
  reply request. Use the explicit lookup button or ask the character to recall
  something.
- **Automatically build memories from new chats** enrolls eligible new messages
  in the local index. With a key configured in Models, it sends their text,
  speaker roles and record times to DeepSeek Flash (low reasoning) at
  `api.deepseek.com` in separate, bounded background requests. This switch
  can run with character lookup off. Unregistered older
  conversations require a separate import.

In **Memory → Review suggestions**, inspect generated statements and their
source quotes, including any stated conditions. Save a supported suggestion as
an independent note or dismiss it. Saving checks the reviewed version and
current source permissions.
Existing comparison proposals can be inspected and dismissed. The desktop
Alpha pauses cold comparison requests and retains existing checkpoints.

With formation off, old permitted notes remain available when use is on. With
use off, the character does not look up memory, and the note manager can still be
used. Turning either switch off retains saved notes. Use the note manager to
correct or forget a note. The forget confirmation lists the notes that will
be removed, including notes removed because they depend on the same evidence.

## Runtime and resources

Install the pinned dependencies with `uv sync --locked --extra memory`. The
default semantic profile uses Jina ONNX Q4 weights under
[CC-BY-NC-4.0](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano-retrieval/blob/ac5d898c8d382b17167c33e5c8af644a3519b47d/README.md).
`uv run --locked --no-sync python tools/prepare_memory_model.py --download`
downloads and verifies the pinned upstream snapshot on request. The source
archive ships the Eidema wheel. Semantic model resources are opt-in downloads.

Session originals, SQLite, Jina inference and candidate assembly run locally.
Warm formation can publish a whole verbatim user report after validating the
source and span, with permission for explicit lookup. Other proposals stay
pending until review. Formation requests use a separate daily budget.
Foreground replies do not wait for a forming or comparing model call.

This SDK uses SQLite schema 20. On upgrade, existing maintenance checkpoints
are imported into the memory database. The old scheduler file stays in place.
For rollback, keep a consistent pre-upgrade copy of the Session files, the
Eidema database, the source-policy sidecar and any previous maintenance
checkpoint. Pending speech is checked against the same source and note
versions used to publish the corresponding reply.

The model provider interface remains open to other hosts and local LLMs. This
release's background configuration uses DeepSeek Flash. Use of a cloud character
for replies is configured separately in Models.

## Evaluation configurations

Published explicit-recall scores use OpenAI GPT-6.1 Terra with reasoning
`none`. The regular OpenAI desktop path uses `low`. The default desktop
role is DeepSeek Flash with thinking disabled, and its opt-in formation task
uses Flash Low separately. [Benchmark protocol and scores](https://github.com/Code-Amadeus/Eidema/blob/main/docs/benchmarks.md)
