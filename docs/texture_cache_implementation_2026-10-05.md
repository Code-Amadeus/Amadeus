# 2 GiB + 60 FPS sampling + automatic BC7 cache

The adopted implementation keeps the 2 GiB retained CPU + potential GPU texture
budget, enables sampling by default at a 60 FPS startup limit, and automatically
derives a persistent BC7 cache on supported HTTP wallpaper surfaces. Explicit
sampling choices win; 30 FPS remains opt-in. A later Wallpaper Engine FPS cap
changes the sampling target but does not overwrite the startup setting.

This follows the [8 GiB comparison](texture_bc7_dgpu_experiment_2026-10-05.md).
That comparison used a manually prebuilt zstd-15 cache. The implementation below
uses bounded on-demand zstd-3 generation and has its own functional evidence;
the earlier 600-second CPU percentages are not relabelled as measurements of
the automatic implementation.

## Ownership and compatibility

- `FrameStore` remains the sole texture owner. The cache does not retain another
  decoded frame collection, change eviction priorities, or release CPU copies.
  Existing Pixi reupload/context recovery continues to use those copies.
- `FrameTextureBackend` negotiates BC7 only on same-origin HTTP(S), with actual
  WebGL BPTC support. A cache hit bypasses Basis initialization/transcoding. An
  invalid cached payload retries the original compressed source once, then
  schedules a replacement after successful GPU upload. Failure to save does
  not reject an uploaded texture.
- `AssetServer` owns source identities, a session write capability, and atomic
  publication. Keys incorporate the actual source contents, bundled Basis
  JS/WASM and BC7 conversion parameters. File metadata memoizes identities
  within one server lifetime; every new server hashes a source on its first
  use. Pack version names alone are not trusted.
- Cache POSTs require the exact same origin, current capability and a previously
  served source key. The client cannot name a destination path. Source changes,
  malformed dimensions/checksums and oversized/incomplete uploads are rejected.
- One encode worker and at most two pending writes bound generation; jobs that
  cannot be admitted are skipped and can be filled on a later load. Two decode
  workers handle hits. Buffers sent for compression are copies, preserving the
  store's CPU restoration data. Worker lifetimes belong to their backend owner.
- The disk target is 4 GiB in the platform user cache. Only validated, flat
  content-addressed files are evicted. Files use temporary-write, fsync and
  atomic replacement; incomplete temporary files are ignored and old owned
  temporary files are cleaned individually. There is no recursive deletion.
  Multiple running app instances may temporarily exceed the target until a
  subsequent write refreshes the shared directory inventory (30-second cadence).
- `RENDER_BC7_CACHE=false` disables the server cache. The Graphics settings page
  exposes both sampling and disk-cache choices. Settings require a backend
  restart. CPU/model-less operation needs no Node subprocess, Python zstd
  installation, model, GPU compute runtime, or character pack for unit tests.
- Legacy `file://` render mode, GPUs without BPTC, and non-single-level/2D
  textures retain the source path. The character package format is unchanged.
  Derived cache data is disposable; older releases simply ignore it.

The zstd worker is vendored from `@bokuweb/zstd-wasm@0.0.27`, with a checked
tarball SHA-512 and per-file provenance. Relative module imports have explicit
extensions for browser loading. The decoder allocates from validated texture
dimensions, not the compressed stream's claimed output size. MIT/BSD notices
are included. The isolated package audit reported zero known vulnerabilities;
production dependency lockfiles and the user's installed environment were not
changed.

## Real GPU functional acceptance

Four serial 60-second fixed-route runs used the RTX 4070 Laptop / ANGLE D3D11,
standard 60 FPS, the existing installed character pack, fresh browser profiles
and one isolated persistent cache directory. These were renderer-only runs,
without ASR/TTS inference. No prebuilt-cache injection was used.

The final run disabled both sampling and disk caching on the same code as the
reuse/repair runs; all measured source hashes match those two runs. It measures
the combined feature benefit relative to the 2 GiB configuration, not relative
to historical unbounded main. Each configuration ran once, in the order empty,
reuse, repair, then disabled baseline; small differences are not statistically
qualified. The empty-cache run preceded the final cache inventory/cleanup
changes, which are captured in its source hashes.

| Playback-period observation | Sampling/cache off | Empty cache + sampling | Restart / reuse | Restart after one corrupt entry |
| --- | ---: | ---: | ---: | ---: |
| Completed UASTC transcodes | 3,411 | 2,710 | 281 | 5 |
| BC7 cache hits | 0 | 49 | 2,476 | 2,755 |
| Completed background writes | 0 | 2,404 | 276 | 3 |
| Skipped writes at queue capacity | 0 | 303 | 4 | 2 |
| Cache read validation failures | 0 | 0 | 0 | 1 (injected) |
| Cache write failures | 0 | 0 | 0 | 0 |
| Texture load failures | 0 | 0 | 0 | 0 |
| Renderer CPU in 60 seconds | 45.02 s | 63.54 s | 20.08 s | 15.72 s |
| Measured Electron + Python host CPU | 80.96 s | 123.52 s | 59.31 s | 52.82 s |
| Renderer private memory peak | 1,656 MiB | 1,713 MiB | 1,751 MiB | 1,692 MiB |
| GPU-process private memory peak (not VRAM) | 1,963 MiB | 2,286 MiB | 2,279 MiB | 2,239 MiB |
| Ticker P99 | 18.6 ms | 18.7 ms | 18.4 ms | 18.4 ms |
| Ticker intervals over 50 ms | 0 | 0 | 0 | 0 |

Relative to the disabled baseline, reuse reduces renderer CPU by 55.4% and
the measured process sum by 26.7%. Once the route's cache is nearly filled,
the repair run reduces those by 65.1% and 34.8%, with 99.85% fewer UASTC
transcodes. The first generation run instead costs 41.1% more renderer CPU
and 52.6% more measured process CPU. All four process groups have complete
CPU intervals; their sum is not whole-system CPU or energy. Renderer memory
remains in the same range, while GPU-process private memory increases; this
short integration comparison does not measure device VRAM. No claim of a
further memory or VRAM reduction follows from keeping the 2 GiB store budget.

The initial cold run performs both conversion and compression, so it costs
additional CPU. The cache is populated progressively; this is not a promise
that the first launch already has warm-cache performance. The later rows show
actual reuse, including filling earlier skipped entries. Approximately 998 MiB
of cache remained after the short routes; the full pack was not prebuilt.

For corruption recovery, one byte of the known idle-frame cache payload was
changed in the isolated test cache after preserving its original. Exactly one
read-validation failure was observed; the rewritten file's SHA-256 matched the
original. The source KTX2 file was not changed.

All four runs completed with zero dropped events and zero source changes
during each run. Companion suppression/restoration, explicit GPU texture
disposal/reupload, and WebGL context-restoration pixel equality passed; GL
errors and source loading failures were zero. The evidence records file hashes
because the implementation was under development rather than attributing the
results to the earlier experiment-only HEAD.

An earlier preflight rejected writes because Windows/Python 3.12 path `stat`
and descriptor `fstat` exposed different `st_ctime` values for the same file.
The owning cache now compares descriptor metadata on both sides. Tests use the
real serving boundary's metadata API. The failed preflight is retained locally;
it is not counted as a passing cache test. A direct GUI launch also lost its
stdout consumer; the four accepted runs used hidden processes with persistent
log redirection.

## Automated checks and remaining scope

- 86 focused Node texture/cache/probe tests passed.
- 200 Electron tests passed; TypeScript and `npm run build` passed using an
  independent source copy with ordinary parent dependency lookup. No junctions
  or dependency reinstalls were used. The build retained the existing large
  bundle warning and an optional runtime-background reference warning.
- 137 related Python checks passed in the final aggregate run. Repository Ruff passed.
- Tests cover source/transcoder invalidation, atomic publication failure,
  quota eviction without touching unrelated or nested files, HTTP write
  authority, actual WASM roundtrips, corrupt-data recovery, cancellation,
  write backpressure and independent backend disposal.
- Graphics settings were rendered from the real card/catalog components with
  synthetic settings and inspected in Chinese. [Before](evidence/texture-cache-ui-2026-10-05/before.png)
  · [After](evidence/texture-cache-ui-2026-10-05/after.png).
- [Sanitized functional evidence](evidence/texture-cache-product-2026-10-05.json).

These 60-second integration checks do not reach the fourth closed-eye visit
and did not certify repair of its earlier approximately 0.62-second gap.
A [subsequent targeted diagnosis and fix](texture_closed_eye_fix_2026-10-05.md)
reproduced the gap with the automatic cache and removed it by bypassing the
duplicate browser HTTP response cache for streamed compressed textures.
Visible Wallpaper Engine/Lively/macOS hosts, model contention and multi-hour
behavior remain unqualified. Keep PR #155 draft.
