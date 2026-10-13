# Hybrid frame-video candidate

This draft exposes the measured frame-video candidate for code review. It is a
precursor to production integration. Video is enabled only by the probe installer;
the shipping loader does not discover video packages. No character media, derived
video, local cache, or raw runtime log is included.

## Scope and runtime ownership

The existing SpriteForge runtime still chooses tags, graph edges, source indices,
speech transitions and exact holds. A registered video descriptor supplies body
textures for that same timeline. Static textures continue to supply entry heads,
the final frame, mouth overlays, required closure poses, explicit indexed holds
and failure recovery. Static playback remains the baseline when no descriptor is
registered. The seam is experimental and is not a supported extension API.

Color uses VP9 CRF 18; a separate lossless, full-range alpha stream is paired by
source index. Each decoder has at most 16 outstanding outputs. Bundle clips load
compressed data on demand; obsolete loads are aborted, and retired callbacks
cannot replace a new source. Display leases preserve the old picture until its
successor is ready. Failures are latched per label and restore static demand.
Aliases share a descriptor only when indexed frame URLs and intervals agree.

**The shared renderer changes are active on the static path too.** They separate
residency pins from loading urgency, allow admissible current requests to preempt
obsolete retention-only work, use short first-hop reserves, align reduced sampling
to the nearest presentation slot, preserve the original canvas inside padded BC7
textures, and permit HTTP connection reuse after a fully consumed cache upload.
These changes need ordinary static-playback regression review even while video is
disabled. Noncancelable backend work continues to occupy its real in-flight slot.

The measured settings are probe overrides: 384 MiB FrameStore, 60 FPS sampling,
50 ms first-hop head demand, a 500 ms current window, and Pixi GPU GC 60/30.
The encoded static head covers at least 306 ms; the selected current window fills
that head during decoder startup. The 50 ms speculative reserve is not the encoded
head length. Shipping defaults remain video off, a 2048 MiB FrameStore, and GPU GC
3600/600. Actual LLM output has not been wired into prefetch: previews in the
experiment are synthetic, revocable hints, and do not choose graph outcomes.

## Evidence and limits

Read the [full acceptance report](frame_video_full_acceptance_2026-10-13.md) and
[structured evidence](evidence/frame-video-full-acceptance-2026-10-13.json).
The corrected pack contained 39 clips / 7552 body frames and 16 mouth images.
Every source PNG and installed KTX2 was checked against the production ledger;
every encoded alpha frame was exact in software. One GPU boundary capture per
clip had exact alpha, minimum visible PSNR 42.3439 dB and SSIM 0.994509.

Matched 600-second routes reduced CPU by 41.1%, private working set by 40.89 MiB
and private commit by 18.54 MiB. Dedicated GPU memory was unchanged; shared GPU
memory increased by 39.81 MiB. This does not establish an order-of-magnitude memory
reduction. Shared GPU counters must not be added to working set as physical RAM.

Both paired resource runs used continuous offscreen CPU readback and each had four
isolated entrance misses. Separate shared-GPU-output controls, releasing every
paint texture, covered all materials and public tags for 190 seconds each without
a playback miss or ticker interval over 50 ms. Those controls support a readback
interference explanation; their resource results are not mixed into the paired
600-second comparison. An earlier 1.067-second supply stall was not reproduced;
its old logs lack request-level timing, so its root cause remains unproven.

Measurements belong to the local experiment revisions recorded in the evidence,
not to a newly measured run on the PR commit. The frozen local snapshot and raw
evidence remain available locally. This publication copies its final code onto
main at `b5a889eccd30301957219d6f8a562f00d62a3ad7`, with no overlapping target
changes. Absolute output paths were made repository-relative. Historical metrics
and hashes were preserved; historical commit IDs are provenance identifiers, not
promises that those commits are published. The draft also updates one obsolete
single-clip test that still expected mouth-configured clips to be rejected.

## Deterministic validation

On the publication branch: 168 focused Node tests, 522 Electron tests, and 62
focused Python tests passed; speech-end handoff, Ruff, architecture checks and the
Electron build passed. Full repository CI and packaged model-less smoke checks
remain pending. No dependency or lockfile change is required. Source-only tests
use synthetic fixtures and do not require character assets or models.

Run from the repository root with the locked Python development environment and
the installed Electron dependencies. The optional offline encoder tests use
FFmpeg/ffprobe with libvpx-vp9 and OpenCV; tests skip unavailable optional tools.
All 62 focused Python tests ran without skips on the publication machine.

```powershell
python -m pytest tests/test_texture_cache.py tests/test_frame_video_encode.py tests/test_frame_video_memory_summary.py tests/test_frame_video_prepare.py tests/test_frame_video_product_summary.py tests/test_frame_video_split.py tests/test_frame_video_visual.py -q
node --test tests/frame_store.test.cjs tests/frame_texture_backend.test.cjs tests/frame_video_probe.test.mjs tests/frame_video_shutdown_control.test.mjs tests/hybrid_fixture.test.mjs tests/hybrid_frame_provider.test.mjs tests/hybrid_lifecycle.test.mjs tests/sprite_frame_sampling.test.cjs tests/sprite_texture_lifecycle.test.cjs tests/sprite_video_source.test.cjs tests/render_frame_gate.test.cjs
node tests/spriteforge_post_speech_handoff.test.js render/web/renderer.js
npm --prefix electron test
npm --prefix electron run build
python -m ruff check .
python tools/architecture/generate_views.py --check
python tools/architecture/generate_readme_overview.py --check
```

## Optional local hardware reproduction

These commands require Windows GPU counters, an appropriate WebCodecs-capable GPU,
and a separately installed pack at `assets/spriteforge/runtime/kurisu` with its
corrected PNG production ledger. These assets are not downloadable from this PR.
`--ledger` must identify the ledger for the installed pack; its records contain
local corrected-PNG paths, installed frame order and PNG/KTX2 hashes. Offline
encoding is diagnostic tooling, not a second production authoring pipeline.

```powershell
python tools/probes/frame-video/full_encode.py --pack assets/spriteforge/runtime/kurisu --ledger C:/character-source/production_manifest.json --output output/diagnostics/video-bundle
```

Use new output directories for every run. Set the interpreter explicitly if the
checkout has no `.venv/Scripts/python.exe`. Run from the repository root:

```powershell
$env:TEXTURE_PROBE_PYTHON = (Resolve-Path .venv/Scripts/python.exe).Path
$electron = (Resolve-Path electron/node_modules/electron/dist/electron.exe).Path
$common = @('sampled', '60', '--duration-seconds', '190', '--texture-budget-mib', '384', '--disk-cache', '--high-performance-gpu', '--texture-gc-trace', '--texture-gc-idle-frames', '60', '--texture-gc-check-frames', '30', '--context-loss', '--texture-disposal', '--shared-texture-output')
node tools/probes/frame-video/memory_run.mjs --electron $electron --probe electron/tests/fpsTextures.probe.mjs --output output/diagnostics/video-materials -- @common --all-materials --video-fixture output/diagnostics/video-bundle --video-lifecycle
node tools/probes/frame-video/memory_run.mjs --electron $electron --probe electron/tests/fpsTextures.probe.mjs --output output/diagnostics/video-tags -- @common --all-clip-tags --tag-preview revoked --video-fixture output/diagnostics/video-bundle --video-lifecycle
python tools/probes/frame-video/compare_bundle.py --fixture output/diagnostics/video-bundle --captures output/diagnostics/video-materials --output output/diagnostics/video-materials-quality
python tools/probes/frame-video/summarize_memory.py output/diagnostics/video-materials
python tools/probes/frame-video/summarize_product.py output/diagnostics/video-materials
```

For a resource comparison, warm a static cache across all materials first; clone
that cache into two separate profiles and verify identical relative filenames,
sizes and SHA-256 values. Pass those profiles via `--cache-profile`. Measure the
same 600-second route twice, changing only whether `--video-fixture` is present.
Keep output mode, GPU, sampling, budget, GC, seed and optional phases identical.
To reproduce the published readback-mode comparison, omit `--shared-texture-output`
from both runs. Report whole-run CPU time, native memory after 20 seconds, transcode
counts, completeness, source hashes, misses and release state. Archive locally;
raw reports may contain local paths and screenshots.

## Review and production gates

Review the five shared product files first: `render/web/renderer.js`,
`render/web/frame_store.js`, `render/web/frame_texture_backend.js`,
`render/server.py` and `render/texture_cache.py`. Then inspect decoder/output
ownership in `tools/probes/frame-video/{decoder,hybrid_source,pixi-output}.mjs`
and the associated contract tests. The remaining probes are evidence tooling.

Before enabling production video: integrate a versioned package through the
formal asset loader and capability selection; validate actual desktop windows,
startup/re-entry, long loops, and real LLM/microphone/TTS interruptions; repeat
balanced resource measurements on that host; perform motion and alpha-edge review
at final display size. Revisit the unresolved historical supply stall with request
timing if it recurs. Merge readiness also requires repository CI.

Keep the single animation controller and the required static resource backend.
Retire the probe installer and superseded duplicate loaders only after the formal
path has live callers and the observation period passes. This draft does not
change roadmap status, enable video by default, or retire static playback.
