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
Render until another explicit request. Render appears once its valid URL arrives,
so failed starts never mount an empty iframe. Backend control buttons now update
the same projection state as the sidebar, while remaining on the Backend page.

## Verification

- Full Electron suite: **317 passed** (`npm test`).
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
