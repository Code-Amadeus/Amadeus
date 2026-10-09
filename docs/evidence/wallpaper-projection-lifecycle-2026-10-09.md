# Wallpaper and Render lifecycle repair — 2026-10-09

Base: main `5a353f0` (including character management #167). Related startup
ownership contract: #110. This repair changes no Host protocol, persisted setting,
character prompt, asset package or shared expression route.

## Problem and ownership

Automatic Wallpaper startup used a separate completion callback. A later Render
choice could run without wallpaper.stop, then the old ready event and reply could
mark both projections active. Callback probes reproduced it on pre-M1 main
`0851af8` and #167. Merely ignoring the old UI reply would leave the Host running.

One renderer controller now owns sidebar toggles, explicit Backend controls and
automatic startup. It serializes Host requests and native-window operations;
after an in-flight start it settles cleanup before the latest requested projection
starts. Automatic startup yields to an earlier manual choice. ready notifications
record the Host fact during an owned start, instead of independently mounting a
second native window. External ready/exit events remain supported through the
same lifecycle; normal exited notifications do not create a stop-RPC loop.

The shared controller replaces App's request and activation guards. The current
intent can change while an operation awaits a reply, but cleanup ownership cannot
be skipped. A failed Wallpaper stop leaves Wallpaper shown as active and blocks
Render until another explicit request. Toggle selection follows the latest user
intent immediately. The embedded Render surface appears only after its valid URL
arrives, so pending or failed starts never mount an empty iframe. Backend control buttons now update
the same projection state as the sidebar, while remaining on the Backend page.

## Verification

- Full Electron suite: **328 passed** (`npm test`).
- `npm run build`: passed.
- Python App-source contracts: **25 passed** (`test_attention_slice.py`,
  `test_auip_experience_surface.py`, `test_project_context_surface.py`).
- Unmodified official model-less desktop smoke: **11/11 passed**. Normal startup,
  authenticated stop, navigation and owned-process shutdown passed.
- Controller regressions cover ready before/after choice, delayed startup/native
  mounts, old failure versus newer choice, cancellation/reopen, stop failure and
  explicit retry, external clients, event-before-reply order, and real App/Backend
  callback routing. The existing Windows cleanup helper tests remain unchanged.

The overlap tests use production controller code with controlled asynchronous
completions. The ordinary smoke does not claim to reproduce exact native-window
race timing or qualify a real Lively installation. No optional model, voice,
Live2D SDK or copyrighted character artwork was copied or committed.


## Review follow-up

The review exposed a mismatch between toggle presentation and toggle input:
Wallpaper could still look on while a stop was pending, and Render could look
off while a start was pending. Both button states now use `wanted`, as `toggle`
does. Chat receives visibility from the confirmed URL instead. A failed stop
restores the actual running selection; an empty Render reply cannot mount a frame.

Explicit Backend Stop requests now retain their target even if the renderer has
no active record, for example after a UI reload. They use the same serialized
cleanup owner and preserve a pending choice of the other projection. The unused
start-parameter constant was removed; the controller remains the sole caller.

Before implementation changes, all original 21 projection tests passed while
nine new review regressions failed. The final projection suite passes **32**
tests, including both directions of explicit-stop/pending-other-start interaction.
The full Electron suite passes **328**, build passes, App-source Python contracts
pass **25**, and a fresh unmodified model-less desktop smoke passes **11/11**.
The deterministic tests inspect the real App JSX bindings as well as controller
state. This smoke does not reproduce the precise Windows cleanup delay or qualify
Lively; no such claim is made.
