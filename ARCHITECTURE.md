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

## Source dependency boundaries

| Layer | Packages |
| --- | --- |
| 0 | `config` |
| 1 | `llm`, `asr`, `tts`, `render`, `wallpaper`, `vts`, `vn_player` |
| 2 | `core`, `agent_host` |
| 3 | `server` (composition root) |

Imports may stay within a layer or point downwards. Existing shared contracts
`server.protocol`, `server.event_bus`, `server.local_auth`,
`core.pyaudio_lifecycle` and `core.turn_coordinator` are explicit exceptions.
Their current package location does not transfer execution authority to their
consumers. Moving them into neutral packages is separate work.

`tests/fixtures/maintainability_baseline.json` records the remaining upward
file/module edges, including function-local imports. New edges fail the ratchet;
removing one does not permit a different edge. Use
`python tools/maintainability_ratchet.py --update` to tighten the inventory.
The same tool reports broad silent exception handlers for manual inspection,
without requiring logging for normal optional cleanup. The configuration read
baseline and intentional process boundaries are documented in `config/README.md`.
