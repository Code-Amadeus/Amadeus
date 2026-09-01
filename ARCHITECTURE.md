# Architecture

Amadeus is a local desktop runtime with four explicit owners:

- Main Chat owns the foreground character conversation and low-frequency decisions.
- Provider Runtime executes delegated work through provider-neutral adapters.
- Work Ledger owns durable projects, work items, attempts, permissions, and completion facts.
- AUIP owns revisioned interaction with attached applications; applications remain the
  authority for their own state and action receipts.

Presentation systems such as Work narration, TTS, Electron Slice, and application
previews render accepted facts. They do not become execution or durable-state owners.

The Host also owns the bounded disclosure boundary when Main Chat delegates to a
model-driven Provider. The authorized Provider task remains the only execution task;
exact current user wording and up to six recent role-labelled User/Main Chat messages
(2000 characters total) may accompany it only as reference evidence. A warm delta is
allowed only for the same typed Provider Session and dialogue source after native
delivery was acknowledged; every missing, ambiguous, clipped, or cross-source cursor
falls back to the current bounded snapshot. The complete contract and its privacy
limits are documented in
[`docs/provider_parent_conversation_handoff_2026-08-31.md`](docs/provider_parent_conversation_handoff_2026-08-31.md).

The code-adjacent diagrams and current migration seams are documented in
[`architecture/README.md`](architecture/README.md). Product and protocol decisions live
under `docs/`; dated experiments are evidence, not automatically current contracts.

The initial public release is a source Alpha. Large changes to Chat, Ledger, narration,
or AUIP should be justified by a failing semantic contract rather than file size alone.
