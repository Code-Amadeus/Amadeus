# VN Lite avatar integration — original Tk window retained

## Final scope

Only the **avatar rendering area** is replaced. VN keeps its original Tk window,
470x226 minimum card composition, 0.88 opacity, borders, colors, caption font and
auto-height layout, outer scan sweep, dragging and right-click close. Slice's
outer frame is unchanged. No Electron process/window, browser shell, new UI toggle
or IPC is added for VN. The earlier whole-window experiment was superseded and its
entrypoints removed; its measurement artifacts remain under diagnostics.

Subsequent visual refinement requested by the user: the existing moving scan lines
are blended 75% toward the card background (25% of their original RGB difference).
Only their fill colors change, once at construction; their geometry, cadence,
caption styling, frame and avatar are unaffected. No new timer/effect is added.

`server/vn_launch_manager.py` now invokes `tools/vn_portrait_overlay_lite.py`,
passing the existing profile's Tk helper explicitly. That adapter subclasses the
original window without copying/modifying its constructor/layout/drag/scan code.
The sibling VN source file is not changed. Normal VN startup automatically selects
Lite when `assets/companion/kurisu/manifest.json` exists. Restart VN to use it;
there is no experimental-renderer checkbox. An already-running external old VN
overlay needs closing first, since the launcher intentionally reuses live overlays.

## What is shared, and what is not

Both entrypoints read the same optional `companion-kurisu` pack: identical RGBA
atlas tiles, crop, selected variants, frame sequences, declared durations and
static-idle alternative. Both select frames by elapsed time (not callback count),
alternate thinking variants between speech runs, and keep at most two decoded
atlases / 16 MiB explicit decoded pixel storage. Increasing FPS does not double
animation speed. The explicit atlas budget does not bound total process RAM.

Canvas and Tk cannot literally execute the same display code. Canvas uses the
existing JS player; Tk uses `render/companion_atlas_tk.py` and one live Tk PhotoImage.
A cross-runtime test drives the **actual JS player** and compares native frame
selection at the same timestamps. Pixel tests verify no extra recrop/tint/alpha
transform. Old per-frame avatar color treatment/sweep is not applied to Lite pixels;
the original scan decoration elsewhere on the window stays unchanged.

The native avatar pauses on unmap, closes atlas images on destruction, and avoids
frame timers for a static tile. The original outer-window timers remain intact.
Default idle is gentle motion; direct adapter launches can use `--static-idle`.

The VN TTS bridge publishes real sentence start/end signals only to its configured
overlay. Captions alone cannot restart speaking; stale sentence completions cannot
stop newer speech. On-loop playback callbacks are scheduled directly before the
cached-subtitle tasks, so a ready second caption cannot overtake its playback identity.
Sentence completion pauses the current speaking pose during the existing 350 ms
neutral-return window, following the main graph runtime's hold/cancel-release
principle. A new sentence cancels that return and can enter the next thinking
variant directly, with no intermediate idle atlas; duplicate starts/stops do not
cycle variants or extend the deadline. With no continuation before the deadline,
the normal idle pose returns. Captions remain intact. This is a bounded sentence-gap
hold, not a claim to know the final sentence of an arbitrarily long silent utterance.
Runner-only events retain their declared duration. Missing Lite media
uses the existing VN cache/legacy renderer; a malformed installed pack fails visibly
rather than silently substituting different artwork. Existing Lite ZIPs are reused
without repackaging or adding a full wallpaper-pack dependency.

## Verification and measured cost

Local artifacts: `output/diagnostics/vn-shared-companion/`.

- `tk-final-verification/result.json`: native verification passed. Original shell
  methods are the same Python functions; window dimensions, opacity, label positions,
  font and colors match the unmodified original. Native speech rendered 20 changed
  frames in 850 ms (~24 FPS). Verified thinking alternation, subtitle-only update,
  350 ms return, stale-stop rejection, stable Tk image count across expression churn,
  two-atlas budget, hidden-avatar pause, release on close, missing-pack fallback and
  static idle without an avatar timer.
- `tk-final-comparison/summary.json`: sequential visible windows, six process-tree
  RSS samples per phase; no game or model. Original Tk: idle 65.6 MiB, speaking
  66.8 MiB, returned 65.9 MiB. Original shell + Lite avatar: idle 59.3 MiB, speaking
  68.2–68.3 MiB, returned 59.5 MiB. Both are a single Python process.
- Short CPU samples were approximately 5–7% of one logical core for the original,
  versus 8–9% for Lite. These short runs show similar memory, **not** a CPU reduction
  or a proven game-FPS benefit. Long-duration gaming acceptance remains unmeasured.

The earlier standalone-Electron 342–362 MiB result is not representative of this
final implementation, nor of an extra Slice window inside an existing desktop
process. The discarded embedded-browser prototype is not installed or selectable.

Run `python -m pytest tests/test_companion_atlas_tk.py tests/test_vn_portrait_shared.py -q`.
Native checks use `tools/probes/verify_vn_lite_tk.py --legacy-helper <VN_ROOT>/vn_portrait_overlay_tk.py --output <FRESH_OUTPUT>`.
Cost comparisons use `tools/probes/compare_vn_portrait_renderers.py --legacy-root <VN_ROOT> --output <FRESH_OUTPUT>`.

This avatar-only integration builds on the earlier Companion Lite PR; that earlier
PR did not include the VN adapter. No unrelated perception, voice-engine or chat
changes are part of this integration.
