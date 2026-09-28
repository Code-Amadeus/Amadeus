# Independent first-pass review — 2026-09-13

Preserve the current visual composition and the frozen DESIGN.md/base.css.
This is a separate corrective run; the original output remains untouched.

The design transferred well: task-centred board, warm subject and restrained
Amadeus controls. Local seven-move win, invalid-move nonmutation, keyboard
controls, post-win reset and the actual Managed-Core seven-move sequence work.
The following observed defects prevent acceptance:

1. **Rendering contradicts mechanics.** At initial state, DOM bounding boxes
   show top-to-bottom disk sizes [3,2,1], but the legal stack is [1,2,3].
   `state.towers` stores bottom-to-top; rendering it directly into a normal
   flex column reverses the meaning. Repair the rendering owner without
   changing the mechanical stack. Check initial, intermediate and completed
   visible order against actual state. This is the primary blocking defect.
2. **Focus contrast on warm material.** The inherited mint outline is very
   weak against the light board. Give `.tower:focus-visible` a dark teal outline
   that contrasts at least 3:1 with the warm board. Preserve mint focus on dark
   controls and keep the selected state distinct from keyboard focus.
3. **Standalone startup errors.** A real file-entry browser with exact SDK
   assets, no Attach query and offline networking emits unhandled page errors
   `AUIP WebSocket failed`. The synchronous try/catch cannot catch a rejected
   `auip.start()` promise. Handle that documented transport startup rejection
   at this app adapter boundary and preserve working standalone interaction.
   Do not change/inspect SDK, invent a Host connection state, silently retry,
   or suppress unrelated application errors. Record the missing transport
   truthfully as an optional integration diagnostic.
4. **Manifest has two hand-maintained sources.** app.js embeds its own object
   even though index.html has the generated `auip-manifest` JSON slot. Read the
   generated slot for AUIP bootstrap; keep `auip.manifest.json` the single
   hand-edited source. Standalone original initialization must still work when
   the adapter bootstrap fails.
5. **The objective projection claims a false fixed sequence.** Hanoi does not
   require permanently completing small/medium/large disks in that order.
   Remove that `sequence/v1` projection and declaration; existing board,
   completed state and legal choices already express the true task. Do not
   invent another state/schema. Mark the reusable completed result as an
   important nonterminal event as required by the authoring contract.

Run the supplied manifest validate/sync/entry preflights again. Verify the
actual entry in a headless browser, normal local and keyboard moves, illegal
move nonmutation, seven-move win/reset, and no unhandled startup error when
the optional transport is unavailable. Do not alter the public move payload.
Do not add a solve action, hints, extra panels, dependencies, SDK edits or git
commits. Record exact verification and remaining limits in AUTHOR_REPORT.md.
