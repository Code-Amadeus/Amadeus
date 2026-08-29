# Architecture

Amadeus is a local desktop runtime with four explicit owners:

- Main Chat owns the foreground character conversation and low-frequency decisions.
- Provider Runtime executes delegated work through provider-neutral adapters.
- Work Ledger owns durable projects, work items, attempts, permissions, and completion facts.
- AUIP owns revisioned interaction with attached applications; applications remain the
  authority for their own state and action receipts.

Presentation systems such as Work narration, TTS, Electron Slice, and application
previews render accepted facts. They do not become execution or durable-state owners.

The code-adjacent diagrams and current migration seams are documented in
[`architecture/README.md`](architecture/README.md). Product and protocol decisions live
under `docs/`; dated experiments are evidence, not automatically current contracts.

The initial public release is a source Alpha. Large changes to Chat, Ledger, narration,
or AUIP should be justified by a failing semantic contract rather than file size alone.
