# Amadeus 0.18 Alpha 1

This source preview integrates the independent [Eidema 0.2 Alpha SDK](https://github.com/Code-Amadeus/Eidema/releases/tag/v0.2.0-alpha.1).

## Memory settings

Open **Settings → General → Memory**. The panel reports the version of Eidema
installed in the backend. The two switches are saved independently and apply
after restarting the backend.

- **Use memory in replies** allows the character to look up previously saved or
  formed notes and permitted conversation evidence. When a cloud character
  reads evidence, its model provider receives the selected material. Use the
  explicit memory lookup button or ask the character to recall something.
- **Form memories from new chats** runs separate, bounded background requests
  to DeepSeek Flash (low reasoning) at `api.deepseek.com` using the DeepSeek API
  key configured in Models. The request includes eligible text, speaker roles
  and record times. Comparison may read earlier evidence already permitted for
  formation. This switch is off by default. It enrolls new committed messages.
  Unregistered older conversations require a separate import.

With formation off, old permitted notes remain available when use is on. With
use off, the character does not look up memory, and the note manager can still be
used. Turning either switch off retains saved notes. Use the note manager to
correct or forget a note.

## Runtime and resources

Install the pinned dependencies with `uv sync --locked --extra memory`. The
default semantic profile uses the separately licensed Jina ONNX Q4 files.
`uv run --locked --no-sync python tools/prepare_memory_model.py --download`
downloads and verifies the pinned upstream snapshot on request. The source
archive ships the Eidema wheel. Semantic model resources are opt-in downloads.

Session originals, SQLite, Jina inference and candidate assembly run locally.
Warm formation can publish a whole verbatim user report after validating the
source and span, with permission for explicit lookup. Rewritten or ambiguous
statements and cold comparisons stay pending. They do not overwrite notes.
Warm and cold share a background budget with an independent cold cadence.
Foreground replies do not wait for a forming or comparing model call.

The model provider interface remains open to other hosts and local LLMs. This
release's background configuration uses DeepSeek Flash. Use of a cloud character
for replies is configured separately in Models.
