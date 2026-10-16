# Automatic frame-video source

Merged in #200 (`2f39036`) and shipped in 0.17.0 Alpha. Automatic frame-source
selection is the default, while older packs retain the existing static path.
Character media is distributed separately.

## Defaults and compatibility

| Effective source | Automatic frame-cache budget | GPU GC | Entry/successor prefetch |
| --- | ---: | --- | --- |
| Old pack, missing/mismatched video, unsupported decoder, or explicit static selection | 2048 MiB | 3600/600 | Original 250/750 ms heads and warm/interactive priorities |
| Matching video package after renderer capability checks | 384 MiB | 60/30 | 50 ms first-hop reserves, current 500 ms window |

Both paths keep the existing BC7 cache option. Its existing default remains on.
The budget accounts for retained static-frame CPU and potential GPU payloads;
it is not a cap on total application memory or decoder surfaces. GPU GC values
are screen-render counts. Static heads in the encoded package cover at least
306 ms; the 50 ms speculative reserve is a different quantity.

Existing Standard, Power saving and Custom graphics profiles, custom frame rate,
pixel-density limit, sampling and disk-cache choices are all preserved. No saved
choice is overwritten. The existing Standard default is 60 FPS with sampling;
manual overrides continue to apply. The new frame-cache selector defaults to
Automatic and also offers explicit 384/768/2048 MiB budgets, which apply to either
source, including fallback. Selecting a smaller static budget is an explicit user
choice, not the fallback default.

The Graphics page shows both saved source/budget choices and actual per-surface
observations through the existing authenticated visual-status channel. Pending
codec checks and fallback reasons remain visible. Saving uses the existing
backend-restart flow. The launcher must not inject hidden graphics overrides.

![Old-pack automatic policy](https://github.com/Code-Amadeus/Amadeus/blob/fd145ee29454389dcade8c64712b1ebeeb349079/docs/images/frame-source/static-default.png)
![Video-active automatic policy](https://github.com/Code-Amadeus/Amadeus/blob/fd145ee29454389dcade8c64712b1ebeeb349079/docs/images/frame-source/video-active.png)

These screenshots use synthetic Host status with the real Settings components;
they contain no character media.

## Installed package layout

Keep the existing `assets/spriteforge/runtime/kurisu` pack and its
`runtime_manifest.json` unchanged. A new package adds an optional `video/`
subdirectory containing `bundle.json` and, for each clip, `clip.json`, `color.ivf`
and `alpha.ivf`. No rename or migration is required for an old pack.

The bundle and every clip must match the installed manifest hash, version,
complete clip set, frame counts and intervals. Required files are checked before
activation; metadata and both stream configurations are checked by the renderer,
and stream bytes are hash-checked before decoding. HTTP mounts expose only those
runtime files. The ordinary character window uses file URLs through Pixi's asset
adapter. Missing or invalid optional video does not invalidate the static pack.

The existing corrected candidate bundle can be installed by copying only these
runtime files into `video/`. The offline `tools/probes/frame-video/full_encode.py` tool can create a new
bundle from a corrected-PNG production ledger and the installed static manifest;
its diagnostic output and source PNGs remain local. Source PNG hashes, installed
KTX2 hashes and ordering must be verified before encoding. No uncorrected source
export should replace the accepted color-corrected lineage.

## Runtime ownership

The existing SpriteForge controller remains the authority for tags, graph paths,
source indices, speech transitions, mouth alignment and exact holds. A single
resource policy selects static supply or paired VP9 color/lossless full-range
alpha supply. Static textures still provide heads, terminal/closure poses, mouth
overlays and indexed holds. Every active decoder has at most 16 outstanding
outputs; compressed clips load lazily and obsolete loads are aborted. Display
leases retain the outgoing image until its successor is ready.

Production provider code lives in `render/web/frame_video`. Probes call the same
implementation. Short prefetch and short GPU GC activate with verified video;
static compatibility restores the previous policy without changing playback
position or consuming graph random draws. Static mode retains floor-based time
sampling; only reduced timelines under the video policy use nearest presentation
slots. Exact holds and mouth anchors remain tied to the displayed source index.
A decoder failure restores static
supply and the corresponding automatic budget; failure remains latched until
reconfiguration/reopening rather than silently retrying forever.

Changing a clip's ordered static URLs or timing invalidates its installed video;
this also restores exact static holds and reports fallback through the existing
source-policy owner. Unchanged bootstrap replay does not invalidate the package.
Asynchronous installation and explicit probe retries check the captured static
clip identities before registration. Returning to video requires package
qualification again; an old descriptor is not automatically reused.

Wallpaper asset revisions include all four video modules (including `ivf.mjs`).
The surface's AssetServer replaces only its own `/frame-video/` file map and
revokes it on static selection, missing/invalid packages or a switch to Live2D.
Other URL prefixes and other surface servers are unaffected.

The shared geometry, cache-connection and FrameStore urgency fixes also apply to
static playback. The original character controller and required static backend
remain live callers and are not retired. Actual LLM output is not wired into
forecast hints; synthetic revocable forecasts remain evidence tooling.

## Existing resource evidence

| Metric | Historical BC7 default: 2 GiB, 190 s fixed route | Matched static: 384 MiB, 600 s full-material route | Matched video: 384 MiB, same 600 s route |
| --- | ---: | ---: | ---: |
| CPU, one-core equivalent | 66.66% | 80.99% | 47.71% |
| Private working set | 1861.67 MiB | 833.78 MiB | 792.89 MiB |
| Private commit | 3603.45 MiB | 1228.12 MiB | 1209.58 MiB |
| Process dedicated GPU memory | 1560.51 MiB | 247.52 MiB | 247.51 MiB |
| Process shared GPU memory | 64.61 MiB | 52.36 MiB | 92.17 MiB |

The historical row shows why the former default's GiB-scale resource cost matters.
It is an existing warm-cache observation, with zero transcodes, not a new run.
Its default GC is inferred from the unchanged source and absent override, rather
than directly sampled. The final pair uses GC 60/30. All rows include offscreen
readback, memory excludes the first 20 seconds, and the historical route/source
differs. **Do not calculate matched savings from the historical column.** Shared
GPU counters must not be added to working set as physical RAM.

Only the last two columns support the paired CPU reduction of 41.1%. Their RAM
and dedicated-GPU difference is small: video's incremental benefit is primarily
CPU once both paths already use the reduced budget and GC policy. The whole
upgrade combines resource policy with video supply; encoding alone is not an
order-of-magnitude memory reduction.

See the [historical record and hashes](https://github.com/Code-Amadeus/Amadeus/blob/fd145ee29454389dcade8c64712b1ebeeb349079/docs/evidence/frame-video-historical-default-2026-10-13.json),
[frozen full-pack report](https://github.com/Code-Amadeus/Amadeus/blob/fd145ee29454389dcade8c64712b1ebeeb349079/docs/frame_video_full_acceptance_2026-10-13.md), and
[full-pack structured evidence](https://github.com/Code-Amadeus/Amadeus/blob/fd145ee29454389dcade8c64712b1ebeeb349079/docs/evidence/frame-video-full-acceptance-2026-10-13.json).
All historical metrics retain their original source revision; they are not newly
measured performance claims for this PR head.

## Validation and remaining gates

`tests/test_frame_video_contracts.py` now includes the video renderer, provider,
fixture, lifecycle, decoder/output and source-policy Node suites in ordinary
pytest discovery. The frozen-display bug has a failing-before/passing-after
reproduction: a healthy frozen video no longer suppresses a failed successor's
static prefetch. Tests also cover missing/invalid packages, capability failure,
explicit overrides, stale asynchronous activation and restoration of original
static prefetch without changing the playback clock. The October 14 audit
regressions reproduce stale video registration, static sampling drift, missing
module revision tracking and retained HTTP mappings without a GPU. They cover
URL order/count and timing changes, active/frozen and inactive alias paths,
asynchronous preparation/retry, 30/60 FPS mouth/hold behavior and mount ownership.
These are deterministic contract checks, not new GPU performance measurements.

Run focused checks with:

```powershell
python -m pytest tests/test_frame_video_contracts.py tests/test_frame_video_package.py tests/test_sprite_texture_lifecycle.py tests/test_sprite_frame_sampling.py tests/test_texture_cache.py tests/test_visual_profile.py tests/test_wallpaper_asset_revision.py tests/test_asset_server_http.py tests/test_system_settings.py tests/test_render_budget.py -q
npm --prefix electron test
npm --prefix electron run build
python -m ruff check .
```

The isolated wallpaper smoke accepts `--production-frame-source` to exercise the
normal VisualProfile/package path; it does not inject the probe video installer.
Use `RENDER_FRAME_SOURCE=static` for a deliberate static run. Normal automatic
activation with the corrected pack was checked for 45 seconds with revoked tag
hints, texture recreation and context restoration. Both HTTP wallpaper and the
ordinary file-based character window activated 384 MiB video; the latter also
returned to 2 GiB static on explicit selection. These are functional checks, not
additional performance baselines. The asset-free settings smoke is
`electron/tests/frameSourceSettings.smoke.mjs`.

The frozen experiments covered 39 clips / 7552 frames and 16 mouths, exact
software alpha and exact GPU alpha at one boundary per clip. Separate
shared-output controls avoided the isolated long ticker gaps seen with continuous
CPU readback. An older 1.067-second supply stall still lacks request-level timing;
successful new runs do not prove its historical cause.

Known limits: actual Lively/desktop presentation, real LLM/microphone/TTS
interruption journeys and final-size motion/alpha review remain unqualified. Other GPUs and webviews are not qualified by the single-GPU
measurements. Automatic capability/failure fallback preserves the old path, but
does not substitute for broader device acceptance.
